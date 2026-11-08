"""Project context is metadata plus observed records, never inferred business completion."""
import hashlib
import re
from datetime import timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from .agent_tasks import ACTIVE, seal, unseal
from .applications import application_out
from .database import get_db
from .models import AgentTask, AIProjectBinding, AuditEvent, CodeProjectDeletion, UserApplication, WorkspaceProject, now
from .project_retirement import require_available_project_path
from .settings import get_settings
from .task_acceptance import acceptance_summary
from .workspace_files import WorkspaceFiles
from .workspace_checkpoints import encode
from .workspace_gateway import own_environment, call_files


PROJECT_ROOT = r'[a-z0-9][a-z0-9-]{0,79}'


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=80)
    goal: str = Field(max_length=1000)
    revision: int = Field(default=0, ge=0)
    request_id: UUID | None = None

    @field_validator('path')
    @classmethod
    def valid_path(cls, value):
        WorkspaceFiles._parts(value)
        return value

    @field_validator('name')
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError('empty name')
        return value.strip()


def public(row, settings):
    return {'id': row.id, 'path': row.project_path, 'name': row.name,
            'goal': unseal(row.encrypted_goal, settings), 'revision': row.revision,
            'created_at': row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at,
            'updated_at': row.updated_at.replace(tzinfo=timezone.utc) if row.updated_at.tzinfo is None else row.updated_at}


def active_workspace_project():
    return (~select(AIProjectBinding.project_id).where(
        AIProjectBinding.project_id == WorkspaceProject.id, AIProjectBinding.state == 'deleted').exists()
        & ~select(CodeProjectDeletion.project_id).where(
            CodeProjectDeletion.project_id == WorkspaceProject.id, CodeProjectDeletion.completed.is_(True)).exists())


def create_project_root(db, principal, settings, path, identifier, forward):
    # Reuse the workspace service's atomic no-replace publish and signed receipt.
    # A retry can recover its own mkdir after a lost response, never adopt an old folder.
    payload = {'entries': {}, 'bytes': 0, 'excluded': []}
    try:
        result = forward(db, principal, settings, 'POST', '/v1/checkpoint-restore', path,
            body=encode({'payload': payload, 'request_id': identifier})).json()
    except HTTPException as error:
        code = ('projects_root_exists'
                if error.status_code == 409 and error.detail == 'File operation could not be completed'
                else 'projects_directory_unavailable')
        raise HTTPException(error.status_code, code) from None
    if result != {'restored': True, 'path': path, 'digest': hashlib.sha256(encode(payload)).hexdigest()}:
        raise HTTPException(503, 'projects_directory_unavailable')


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/projects', tags=['workspace-projects'])

    def owned(db, principal, identifier):
        env = own_environment(db, principal)
        row = db.scalar(select(WorkspaceProject).where(WorkspaceProject.id == str(identifier),
            WorkspaceProject.environment_id == env.id, active_workspace_project()))
        if row is None:
            raise HTTPException(404, 'projects_not_found')
        return env, row

    @router.get('')
    def listing(principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        rows = db.scalars(select(WorkspaceProject).where(WorkspaceProject.environment_id == env.id,
                          active_workspace_project())
                          .order_by(WorkspaceProject.updated_at.desc(), WorkspaceProject.id))
        return {'projects': [public(row, get_settings()) for row in rows]}

    @router.put('')
    def save(body: ProjectInput, principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        require_available_project_path(db, env.id, body.path)
        if env.status == 'suspended':
            raise HTTPException(403, 'projects_account_suspended')
        settings = get_settings()
        row = db.scalar(select(WorkspaceProject).where(WorkspaceProject.environment_id == env.id,
            WorkspaceProject.project_path == body.path))
        binding = db.get(AIProjectBinding, row.id) if row else None
        if binding and binding.state in ('deleting', 'deleted'):
            raise HTTPException(409, 'projects_changed')
        identifier = None
        if body.request_id:
            if body.revision or not re.fullmatch(PROJECT_ROOT, body.path):
                raise HTTPException(400, 'projects_invalid_request')
            identifier = str(uuid5(NAMESPACE_URL, env.id + '/code-project/' + str(body.request_id)))
            previous = db.get(WorkspaceProject, identifier)
            if previous:
                if (previous.project_path != body.path or previous.name != body.name or
                        unseal(previous.encrypted_goal, settings) != body.goal):
                    raise HTTPException(409, 'projects_request_conflict')
                return public(previous, settings)
            if row:
                raise HTTPException(409, 'projects_root_in_use')
        if row and row.name == body.name and unseal(row.encrypted_goal, settings) == body.goal and body.revision in (row.revision, row.revision - 1):
            return public(row, settings)
        if body.revision != (row.revision if row else 0):
            raise HTTPException(409, 'projects_changed')
        if row is None:
            if not re.fullmatch(PROJECT_ROOT, body.path):
                raise HTTPException(400, 'projects_invalid_request')
            if db.scalar(select(func.count()).select_from(WorkspaceProject).where(
                    WorkspaceProject.environment_id == env.id, active_workspace_project())) >= settings.workspace_projects_limit:
                raise HTTPException(409, 'projects_limit')
            identifier = identifier or str(uuid5(NAMESPACE_URL, env.id + '/workspace-directory/' + body.path))
            create_project_root(db, principal, settings, body.path, identifier, call_files)
            row = WorkspaceProject(id=identifier, environment_id=env.id, project_path=body.path, revision=0)
            db.add(row)
        row.name, row.encrypted_goal = body.name, seal(body.goal, settings)
        row.revision += 1
        row.updated_at = now()
        db.flush()
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                         action='project.context.saved', target_id=f'{row.id}/{row.revision}'))
        db.commit()
        return public(row, settings)

    @router.get('/{project_id}')
    def context(project_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, row = owned(db, principal, project_id)
        settings = get_settings()
        rows = list(db.scalars(select(AgentTask).where(AgentTask.environment_id == env.id,
            AgentTask.config['project_path'].as_string() == row.project_path)
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc()).limit(31)))
        tasks = []
        for task in rows[:30]:
            record = task.resources or {}
            request = unseal(record['user_request'], settings) if record.get('user_request') else None
            tasks.append({'id': task.id, 'conversation_id': task.conversation_id,
                'title': request['text'][:160] if request else '', 'status': task.status,
                'created_at': task.created_at, 'acceptance': acceptance_summary(task)})
        app = db.scalar(select(UserApplication).where(UserApplication.environment_id == env.id,
            UserApplication.project_path == row.project_path, UserApplication.status != 'deleted'))
        return {**public(row, settings), 'tasks': tasks, 'tasks_truncated': len(rows) > 30,
                'application': application_out(app) if app else None}

    @router.get('/{project_id}/source')
    def source(project_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, row = owned(db, principal, project_id)
        if db.scalar(select(AgentTask.id).where(AgentTask.environment_id == env.id,
                AgentTask.status.in_(ACTIVE)).limit(1)):
            raise HTTPException(409, 'projects_task_active')
        try:
            content = call_files(db, principal, get_settings(), 'GET', '/v1/context', row.project_path).content
        except HTTPException as error:
            raise HTTPException(error.status_code, 'projects_source_unavailable') from None
        digest = hashlib.sha256(content).hexdigest()
        app = db.scalar(select(UserApplication).where(UserApplication.environment_id == env.id,
            UserApplication.project_path == row.project_path, UserApplication.status != 'deleted'))
        release = (app.resources.get('last_good') or {}).get('provenance') if app else None
        previous = (release or {}).get('source_sha256')
        return {'source_sha256': digest, 'release_revision': (release or {}).get('revision'),
                'state': ('same' if digest == previous else 'changed') if previous else 'unbound',
                'observed_at': now()}

    return router
