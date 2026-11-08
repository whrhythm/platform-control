"""Explicit template creation, isolated sample preview and source-bound manual checks."""
import hashlib
import io
import json
import secrets
import tarfile
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func

from .agent_tasks import ACTIVE, seal, unseal
from .database import get_db
from .models import AgentTask, AuditEvent, WorkspaceProject, BusinessTemplate, ProjectDatabase, UserApplication, now
from .project_databases import ensure_database, public_database
from .settings import get_settings
from .workspace_gateway import call_files, own_environment
from .workspace_projects import active_workspace_project, create_project_root, public as project_out
from .templates.support.app import password_hash

ASSETS = Path(__file__).parent / 'templates' / 'support'
CHECKS = ['register', 'assign', 'filter', 'preview_boundary']


class Create(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    name: str = Field(min_length=1, max_length=80)


class Acceptance(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    checks: list[str] = Field(max_length=4)
    revision: int = Field(ge=0)


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/business-templates', tags=['business-templates'])

    def own(db, principal, project_id=None):
        settings = get_settings()
        if not settings.business_templates_enabled: raise HTTPException(404, 'templates_disabled')
        env = own_environment(db, principal); db.refresh(env, with_for_update=True)
        if env.status == 'suspended': raise HTTPException(403, 'templates_account_suspended')
        if project_id is None: return env, settings
        project = db.get(WorkspaceProject, str(project_id))
        template = db.get(BusinessTemplate, str(project_id))
        if not project or project.environment_id != env.id or not template:
            raise HTTPException(404, 'templates_not_found')
        return env, settings, project, template

    def snapshot(db, principal, env, settings, project):
        if db.scalar(select(AgentTask.id).where(AgentTask.environment_id == env.id, AgentTask.status.in_(ACTIVE)).limit(1)):
            raise HTTPException(409, 'templates_agent_active')
        try: content = call_files(db, principal, settings, 'GET', '/v1/context', project.project_path).content
        except HTTPException: raise HTTPException(503, 'templates_source_unavailable') from None
        return content, hashlib.sha256(content).hexdigest()

    def output(db, settings, project, template):
        database = db.scalar(select(ProjectDatabase).where(ProjectDatabase.environment_id == project.environment_id,
            ProjectDatabase.project_path == project.project_path, ProjectDatabase.status != 'deleted'))
        app = db.scalar(select(UserApplication).where(UserApplication.environment_id == project.environment_id,
            UserApplication.project_path == project.project_path, UserApplication.status != 'deleted'))
        release = (app.resources.get('last_good') or {}).get('provenance', {}) if app else {}
        return {'project': project_out(project, settings), 'template_id': template.template_id,
            'state': template.state, 'database': public_database(database) if database else None,
            'acceptance': template.acceptance, 'release_acceptance': release.get('template_acceptance'),
            'release_source_sha256': release.get('source_sha256')}

    @router.get('')
    def catalog(principal=Depends(principal_dependency), db=Depends(get_db)):
        own_environment(db, principal)
        return {'enabled': get_settings().business_templates_enabled,
            'templates': [{'id': 'support', 'version': '1', 'name': '售后工单'}] if get_settings().business_templates_enabled else []}

    @router.post('', status_code=201)
    def create(body: Create, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings = own(db, principal)
        identifier = str(uuid5(NAMESPACE_URL, env.id + '/support/' + str(body.request_id)))
        project = db.get(WorkspaceProject, identifier)
        if project:
            if project.name != body.name.strip(): raise HTTPException(409, 'templates_request_conflict')
            return output(db, settings, project, db.get(BusinessTemplate, identifier))
        if not body.name.strip(): raise HTTPException(422, 'templates_invalid_name')
        if db.scalar(select(func.count()).select_from(WorkspaceProject).where(
                WorkspaceProject.environment_id == env.id, active_workspace_project())) >= settings.workspace_projects_limit:
            raise HTTPException(409, 'templates_project_limit')
        path = 'support-' + UUID(identifier).hex[:16]
        if db.scalar(select(WorkspaceProject.id).where(
                WorkspaceProject.environment_id == env.id, WorkspaceProject.project_path == path)):
            raise HTTPException(409, 'templates_file_conflict')
        try:
            create_project_root(db, principal, settings, path, identifier, call_files)
        except HTTPException as error:
            code = 'templates_file_conflict' if error.detail == 'projects_root_exists' else 'templates_files_unavailable'
            raise HTTPException(error.status_code, code) from None
        password = secrets.token_urlsafe(24)
        access = {'username': 'owner', 'password': password, 'password_hash': password_hash(password)}
        project = WorkspaceProject(id=identifier, environment_id=env.id,
            project_path=path, name=body.name.strip(),
            encrypted_goal=seal('登记、分配和跟进售后工单；邀请同事使用，修改应用后保留业务数据。', settings))
        db.add(project); db.flush()
        template = BusinessTemplate(project_id=identifier, template_id='support', version='1',
            encrypted_access=seal(access, settings))
        db.add(template)
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
            action='template.requested', target_id=identifier))
        db.commit()
        return output(db, settings, project, template)

    @router.get('/{project_id}')
    def status(project_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        _, settings, project, template = own(db, principal, project_id)
        return output(db, settings, project, template)

    @router.post('/{project_id}/prepare')
    def prepare(project_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings, project, template = own(db, principal, project_id)
        if template.state == 'preparing':
            access = unseal(template.encrypted_access, settings)
            files = {name: (ASSETS / name).read_bytes() for name in ('app.py', 'index.html', 'Dockerfile', 'README.md')}
            files['settings.json'] = json.dumps({'title': project.name, 'version': '1',
                'owner_password_hash': access['password_hash']}, ensure_ascii=False, indent=2).encode()
            expected = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
            if template.manifest and template.manifest != expected: raise HTTPException(409, 'templates_version_changed')
            template.manifest = expected; db.commit()
            try:
                try: call_files(db, principal, settings, 'POST', '/v1/directories', project.project_path)
                except HTTPException as error:
                    if error.status_code != 409: raise
                for name, content in files.items():
                    try: call_files(db, principal, settings, 'PUT', '/v1/file', project.project_path+'/'+name, body=content)
                    except HTTPException as error:
                        if error.status_code != 409: raise
                        existing = call_files(db, principal, settings, 'GET', '/v1/file', project.project_path+'/'+name).content
                        if hashlib.sha256(existing).hexdigest() != expected[name]:
                            raise HTTPException(409, 'templates_file_conflict') from None
            except HTTPException as error:
                raise HTTPException(error.status_code, 'templates_file_conflict' if error.status_code == 409 else 'templates_files_unavailable') from None
            template.state = 'files_ready'; db.commit()
        try: ensure_database(db, principal, project.project_path)
        except HTTPException as error:
            raise HTTPException(error.status_code, 'templates_database_allowance' if error.detail == 'database_allowance_exceeded' else 'templates_database_unavailable') from None
        return output(db, settings, project, template)

    @router.get('/{project_id}/preview')
    def preview(project_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings, project, _ = own(db, principal, project_id)
        content, digest = snapshot(db, principal, env, settings, project)
        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode='r:*') as archive:
                member = next(item for item in archive.getmembers() if item.name in ('index.html', './index.html'))
                if not member.isfile() or member.size > 512*1024: raise ValueError()
                html = archive.extractfile(member).read().decode('utf-8')
                config_member = next(item for item in archive.getmembers() if item.name in ('settings.json', './settings.json'))
                if not config_member.isfile() or config_member.size > 8192: raise ValueError()
                title = json.loads(archive.extractfile(config_member).read())['title']
                if not isinstance(title, str) or len(title) > 160: raise ValueError()
        except (ValueError, KeyError, TypeError, StopIteration, tarfile.TarError, UnicodeError):
            raise HTTPException(409, 'templates_preview_unavailable') from None
        # Opaque iframe origin, no network, no forms, no top navigation. User HTML is never trusted.
        csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'"
        return {'html': '<meta http-equiv="Content-Security-Policy" content="'+csp+'"><script>window.__SUPPORT_PREVIEW__=true;window.__SUPPORT_TITLE__='+json.dumps(title).replace('<','\\u003c')+';</script>'+html,
            'source_sha256': digest}

    @router.post('/{project_id}/access')
    def access(project_id: UUID, response: Response, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings, project, template = own(db, principal, project_id)
        response.headers['Cache-Control'] = 'no-store'
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
            action='template.access.viewed', target_id=project.id)); db.commit()
        value = unseal(template.encrypted_access, settings)
        return {'username': value['username'], 'password': value['password']}

    @router.put('/{project_id}/acceptance')
    def acceptance(project_id: UUID, body: Acceptance, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings, project, template = own(db, principal, project_id)
        if sorted(body.checks) != sorted(CHECKS): raise HTTPException(422, 'templates_checks_incomplete')
        _, digest = snapshot(db, principal, env, settings, project)
        if digest != body.source_sha256: raise HTTPException(409, 'templates_source_changed')
        current = template.acceptance
        if current.get('source_sha256') == digest and current.get('checks') == body.checks:
            return output(db, settings, project, template)
        if body.revision != current.get('revision', 0): raise HTTPException(409, 'templates_acceptance_changed')
        template.acceptance = {'revision': body.revision+1, 'source_sha256': digest, 'checks': body.checks,
            'updated_at': now().isoformat(), 'basis': 'owner_sample_preview'}
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
            action='template.preview.accepted', target_id=project.id)); db.commit()
        return output(db, settings, project, template)

    return router
