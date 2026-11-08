"""Retire code projects through the existing UID-checked deployment and database controllers."""
from hashlib import sha256
import json
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from .agent_tasks import ACTIVE, seal
from .ai_project_deletion import Advance, folder_preview, inspect_owned_folder, remove_planned_folder
from .ai_projects import Owner
from .application_runtime import application_origin, app_name
from .database import get_db
from .models import (AgentTask, AIProjectBinding, AuditEvent, CodeProjectDeletion, ProjectDatabase,
    UserApplication, UserRegistry, WorkspaceProject, now)
from .project_databases import names
from .settings import get_settings
from .workspace_gateway import own_environment


class CodeAdvance(Advance):
    confirm_code: bool = False
    retry_failed: bool = False


def related_resources(db, project):
    app = db.scalar(select(UserApplication).where(UserApplication.environment_id == project.environment_id,
        UserApplication.project_path == project.project_path).with_for_update())
    database = db.scalar(select(ProjectDatabase).where(ProjectDatabase.environment_id == project.environment_id,
        ProjectDatabase.project_path == project.project_path).with_for_update())
    return app, database


def overlapping_resource(db, project):
    for model in (UserApplication, ProjectDatabase):
        paths = db.scalars(select(model.project_path).where(model.environment_id == project.environment_id,
                                                           model.status != 'deleted'))
        if any(path != project.project_path and
               (path.startswith(project.project_path + '/') or project.project_path.startswith(path + '/'))
               for path in paths):
            return True
    return bool(db.scalar(select(AgentTask.id).where(AgentTask.environment_id == project.environment_id,
                                                     AgentTask.status.in_(ACTIVE)).limit(1)))


def shared_database(db, project, database):
    secret = names(database)[2]
    for app in db.scalars(select(UserApplication).where(UserApplication.environment_id == project.environment_id,
            UserApplication.project_path != project.project_path, UserApplication.status != 'deleted')):
        if secret in (app.resources.get('database_secret'), (app.resources.get('last_good') or {}).get('database_secret')):
            return True
    return False


def plan_deletion(db, principal, project, app, database):
    if app and app.status in ('queued', 'preparing', 'building', 'deploying', 'changing'):
        raise HTTPException(409, 'ai_projects_deletion_busy')
    if database and database.status in ('requested', 'provisioning'):
        raise HTTPException(409, 'ai_projects_deletion_busy')
    if app and app.error_code and 'requires_review' in app.error_code:
        raise HTTPException(409, 'ai_projects_deletion_review')
    if (app and app.status == 'failed' and
            app.resources.get('cleanup', {}).get('status') != 'complete'):
        raise HTTPException(409, 'ai_projects_deletion_busy')
    if overlapping_resource(db, project):
        folder = {'reason': 'in_use'}
        # Directory provenance still determines whether the attached database
        # is dedicated. Avoid any destructive action on overlapping resources.
        dedicated = False
    else:
        folder = inspect_owned_folder(db, principal, project)
        dedicated = bool(folder.get('scope'))
    remove_db = bool(database and database.status != 'deleted' and dedicated and
                     not shared_database(db, project, database))
    if database and database.status != 'deleted' and not remove_db and dedicated:
        folder = {'reason': 'in_use'}
    if remove_db and database.error_code == 'database_ownership_requires_review':
        raise HTTPException(409, 'ai_projects_deletion_review')
    deployment = None
    images = []
    repository = None
    if app:
        last = app.resources.get('last_good') or {}
        images = sorted({value for value in (app.image, last.get('image')) if value})
        registry = db.get(UserRegistry, project.environment_id)
        if registry:
            repository = registry.project_name + '/' + app_name(app)
        if app.status != 'deleted':
            route = app.resources.get('workloads', {}).get('ApisixRoute', {})
            domain = urlsplit(app.url or last.get('url') or application_origin(app, get_settings())).hostname
            deployment = {'id': app.id, 'revision': app.revision,
                          'domain': domain if route.get('state') in ('created', 'deleting') or app.url else None}
    db_plan = None
    if database and database.status != 'deleted':
        db_plan = {'id': database.id, 'generation': database.generation, 'remove': remove_db}
    return {'folder': project.project_path, 'folder_cleanup': folder, 'deployment': deployment,
            'database': db_plan, 'images': images, 'image_repository': repository}


def preview(plan):
    return {'state': 'confirm', 'kind': 'code',
        'confirmation': sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
        'folder': plan['folder'], **folder_preview(plan['folder_cleanup']),
        'trials': 0, 'releases': 0, 'shares': 0, 'knowledge_libraries': 0,
        'knowledge_revisions': 0, 'preserved_libraries': 0,
        'code': {'deployment': bool(plan['deployment']),
            'domain': plan['deployment']['domain'] if plan['deployment'] else None,
            'database': ('delete' if plan['database']['remove'] else 'keep') if plan['database'] else 'none',
            'images': plan['images'], 'image_repository': plan['image_repository']}}


def progress(db, deletion, *, advance=False, retry=False):
    if deletion.completed:
        return {'state': 'deleted', **deletion.plan}
    plan = deletion.plan
    deployment = plan['deployment']
    if deployment:
        app = db.get(UserApplication, deployment['id'], with_for_update=advance)
        if not app or app.revision != deployment['revision'] or app.desired_state != 'deleted':
            raise HTTPException(409, 'ai_projects_deletion_review')
        if app.status != 'deleted':
            if advance and app.status == 'failed':
                if app.error_code and 'requires_review' in app.error_code:
                    raise HTTPException(409, 'ai_projects_deletion_review')
                if not retry:
                    raise HTTPException(503, 'ai_projects_deletion_retry')
                app.status, app.error_code = 'changing', None
            return {'state': 'deployment'}
    database = plan['database']
    if database and database['remove']:
        row = db.get(ProjectDatabase, database['id'], with_for_update=advance)
        if not row or row.generation != database['generation']:
            raise HTTPException(409, 'ai_projects_deletion_review')
        if row.status != 'deleted':
            if advance:
                if row.status in ('requested', 'provisioning') or row.error_code == 'database_ownership_requires_review':
                    raise HTTPException(409, 'ai_projects_deletion_review')
                if row.status == 'failed' and row.resources.get('deletion_requested') and not retry:
                    raise HTTPException(503, 'ai_projects_deletion_retry')
                if row.status != 'deleting':
                    project = db.get(WorkspaceProject, deletion.project_id)
                    if shared_database(db, project, row):
                        raise HTTPException(409, 'ai_projects_deletion_review')
                    row.status, row.error_code = 'deleting', None
                    row.resources = {**row.resources, 'deletion_requested': True}
            return {'state': 'database'}
    return {'state': 'native'}


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/projects', tags=['code-project-deletion'])

    def owned(db, principal, identifier):
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        project = db.get(WorkspaceProject, str(identifier))
        if (not project or project.environment_id != env.id or db.get(AIProjectBinding, project.id)):
            raise HTTPException(404, 'ai_projects_not_found')
        return env, project, db.get(CodeProjectDeletion, project.id)

    def audit(db, principal, env, project, action):
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                          action='code_project.' + action, target_id=project.id))

    @router.get('/{identifier}/deletion')
    def inspect(identifier: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        _, project, deletion = owned(db, principal, identifier)
        return progress(db, deletion) if deletion else preview(plan_deletion(
            db, principal, project, *related_resources(db, project)))

    @router.post('/{identifier}/deletion')
    def advance(identifier: UUID, body: CodeAdvance, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, project, deletion = owned(db, principal, identifier)
        if deletion is None:
            app, database = related_resources(db, project)
            plan = plan_deletion(db, principal, project, app, database)
            if (not body.confirm_code or preview(plan)['confirmation'] != body.confirmation or
                    bool(plan['folder_cleanup'].get('scope')) != body.remove_folder):
                raise HTTPException(409, 'ai_projects_deletion_changed')
            deletion = CodeProjectDeletion(project_id=project.id, plan=plan, completed=False)
            db.add(deletion)
            if plan['deployment']:
                app.desired_state, app.status, app.error_code = 'deleted', 'changing', None
            audit(db, principal, env, project, 'deletion_started')
        result = progress(db, deletion, advance=True, retry=body.retry_failed)
        db.commit()
        return result

    @router.post('/{identifier}/deletion/complete')
    def complete(identifier: UUID, body: Owner, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, project, deletion = owned(db, principal, identifier)
        if not deletion:
            raise HTTPException(409, 'ai_projects_deletion_not_ready')
        state = progress(db, deletion)
        if deletion.completed:
            return state
        if state['state'] != 'native':
            raise HTTPException(409, 'ai_projects_deletion_not_ready')
        scope = deletion.plan['folder_cleanup'].get('scope')
        retained = bool(scope and (overlapping_resource(db, project) or
                                   not remove_planned_folder(db, principal, project, scope)))
        if scope:
            audit(db, principal, env, project, 'root_preserved' if retained else 'root_removed')
        project.project_path, project.name = '@deleted/' + project.id, ''
        project.encrypted_goal, project.updated_at = seal('', get_settings()), now()
        deletion.completed = True
        deletion.plan = {'folder_retained': True} if retained else {}
        audit(db, principal, env, project, 'deletion_completed')
        db.commit()
        return {'state': 'deleted', **deletion.plan}

    return router
