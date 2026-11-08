"""Coordinate private LibreChat Agent creation without cross-database transactions."""
from uuid import UUID, NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .agent_tasks import seal, unseal
from .ai_policy import effective_policy
from .database import get_db
from .models import AIProjectBinding, AuditEvent, WorkspaceProject, now
from .settings import get_settings
from .workspace_gateway import call_files, own_environment
from .workspace_projects import PROJECT_ROOT, active_workspace_project, create_project_root, public as project_out


class Create(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: UUID
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=1000)
    # Stamped by the authenticated LibreChat BFF, not accepted from browser JSON.
    native_owner_id: str = Field(pattern=r'^[0-9a-f]{24}$')
    path: str | None = Field(default=None, pattern='^' + PROJECT_ROOT + '$')


class Owner(BaseModel):
    model_config = ConfigDict(extra='forbid')
    native_owner_id: str = Field(pattern=r'^[0-9a-f]{24}$')


class Confirmation(Owner):
    agent_id: str = Field(pattern=r'^agent_[0-9a-f]{32}$')


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/ai-projects', tags=['ai-projects'])

    def owned(db, principal, project_id=None):
        settings = get_settings()
        if not settings.ai_projects_enabled:
            raise HTTPException(404, 'ai_projects_disabled')
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        if env.status == 'suspended':
            raise HTTPException(403, 'ai_projects_account_suspended')
        if project_id is None:
            return env, settings
        project = db.get(WorkspaceProject, str(project_id))
        binding = db.get(AIProjectBinding, str(project_id))
        if (not project or project.environment_id != env.id or not binding or
                binding.state not in ('preparing', 'ready')):
            raise HTTPException(404, 'ai_projects_not_found')
        return env, settings, project, binding

    def output(settings, project, binding, *, initial=False):
        value = {'project': project_out(project, settings), 'agent_id': binding.agent_id,
                 'state': binding.state, 'knowledge_state': 'ready' if binding.knowledge_revision_id else 'not_configured'}
        if initial:
            value['initial_agent'] = unseal(binding.encrypted_initial, settings)
        return value

    def audit(db, env, principal, action, identifier):
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer,
            subject=principal.subject, action=action, target_id=identifier))

    @router.get('')
    def listing(principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        settings = get_settings()
        if not settings.ai_projects_enabled:
            return {'enabled': False, 'projects': []}
        rows = db.execute(select(WorkspaceProject, AIProjectBinding).join(AIProjectBinding)
            .where(WorkspaceProject.environment_id == env.id, AIProjectBinding.state != 'deleted')
            .order_by(WorkspaceProject.created_at.desc())).all()
        return {'enabled': True, 'projects': [output(settings, p, b) for p, b in rows]}

    @router.post('', status_code=201)
    def create(body: Create, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings = owned(db, principal)
        identifier = str(uuid5(NAMESPACE_URL, env.id + '/knowledge-assistant/' + str(body.request_id)))
        project = db.get(WorkspaceProject, identifier)
        if project:
            binding = db.get(AIProjectBinding, identifier)
            if (not binding or binding.state not in ('preparing', 'ready') or
                    binding.native_owner_id != body.native_owner_id or
                    (body.path is not None and project.project_path != body.path) or
                    project.name != body.name or unseal(project.encrypted_goal, settings) != body.purpose):
                raise HTTPException(409, 'ai_projects_request_conflict')
            return output(settings, project, binding)
        if db.scalar(select(func.count()).select_from(WorkspaceProject).where(
                WorkspaceProject.environment_id == env.id, active_workspace_project())) >= settings.workspace_projects_limit:
            raise HTTPException(409, 'ai_projects_limit')
        policy = effective_policy(db, settings)
        if not policy['chat_model'] or policy['chat_model'] not in policy['allowed_models']:
            raise HTTPException(503, 'ai_projects_model_unavailable')
        path = body.path or 'assistant-' + UUID(identifier).hex[:16]
        if db.scalar(select(WorkspaceProject.id).where(
            WorkspaceProject.environment_id == env.id, WorkspaceProject.project_path == path
        )):
            raise HTTPException(409, 'ai_projects_root_in_use')
        try:
            create_project_root(db, principal, settings, path, identifier, call_files)
        except HTTPException as error:
            raise HTTPException(error.status_code, 'ai_' + error.detail) from None
        initial = {'name': body.name, 'description': body.purpose, 'provider': 'LiteLLM',
            'model': policy['chat_model'], 'tools': [], 'skills_enabled': False,
            'instructions': '你是这个项目的业务助手，项目用途：' + body.purpose +
                '\n当前尚未配置知识检索。不要声称已读取用户文件或提供虚构引用。'
                '没有依据时说明限制，不假装已执行工具、发布应用或处理业务记录。'}
        project = WorkspaceProject(id=identifier, environment_id=env.id,
            project_path=path, name=body.name,
            encrypted_goal=seal(body.purpose, settings))
        db.add(project)
        db.flush()
        binding = AIProjectBinding(project_id=identifier, environment_id=env.id,
            request_id=str(body.request_id), native_owner_id=body.native_owner_id,
            agent_id='agent_' + UUID(identifier).hex, encrypted_initial=seal(initial, settings))
        db.add(binding)
        audit(db, env, principal, 'ai_project.requested', identifier)
        db.commit()
        return output(settings, project, binding)

    @router.post('/{project_id}/prepare')
    def prepare(project_id: UUID, body: Owner, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings, project, binding = owned(db, principal, project_id)
        if binding.native_owner_id != body.native_owner_id:
            raise HTTPException(409, 'ai_projects_owner_mismatch')
        initial = unseal(binding.encrypted_initial, settings)
        if initial['model'] not in effective_policy(db, settings)['allowed_models']:
            raise HTTPException(409, 'ai_projects_model_unavailable')
        # File creation is separately idempotent and never writes existing files.
        try:
            call_files(db, principal, settings, 'GET', '/v1/directories', project.project_path)
        except HTTPException as error:
            if error.status_code != 404:
                raise HTTPException(503, 'ai_projects_space_unavailable') from None
            try:
                call_files(db, principal, settings, 'POST', '/v1/directories', project.project_path)
            except HTTPException:
                raise HTTPException(503, 'ai_projects_space_unavailable') from None
        return output(settings, project, binding, initial=True)

    @router.post('/{project_id}/confirm')
    def confirm(project_id: UUID, body: Confirmation, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings, project, binding = owned(db, principal, project_id)
        if binding.native_owner_id != body.native_owner_id or binding.agent_id != body.agent_id:
            raise HTTPException(409, 'ai_projects_binding_mismatch')
        if binding.state != 'ready':
            binding.state, binding.updated_at = 'ready', now()
            audit(db, env, principal, 'ai_project.bound', project.id)
            db.commit()
        return output(settings, project, binding)

    return router
