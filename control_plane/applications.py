"""User-facing application intents; provider writes belong to the controller."""
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .application_runtime import WRITE_REVIEW_CODE
from .database import get_db
from .models import AuditEvent, UserApplication, ProjectDatabase, UserObjectStorage
from .object_storage import secret_name as object_secret_name
from .project_databases import names as database_names
from .settings import get_settings
from .workspace_gateway import own_environment, call_files
from .workspace_files import WorkspaceFiles, InvalidWorkspacePath
from .capacity import application_limit
from .application_resources import active_resources
from .plan_quotas import active_quotas, admit_application
from .release_provenance import freeze, application_provenance


def application_out(app):
    result = {key: getattr(app, key) for key in ('id', 'project_path', 'status', 'desired_state', 'image', 'url', 'error_code', 'observation')}
    result['cleanup'] = app.resources.get('cleanup')
    result.update(application_provenance(app, get_settings()))
    result['previous_url'] = app.resources.get('last_good', {}).get('url') if app.status == 'failed' and app.resources.get('cleanup', {}).get('runtime') == 'previous_release' else None
    return result


def release_database(app, database):
    release = app.resources.get('last_good') or {}
    same_release = app.status in ('running', 'stopped') and release.get('revision') == app.revision
    rolled_back = app.status == 'failed' and app.resources.get('cleanup', {}).get('runtime') == 'previous_release'
    if not database or not (same_release or rolled_back) or release.get('database_secret') != database_names(database)[2]:
        return None
    return {key: getattr(database, key) for key in ('reserved_bytes', 'used_bytes', 'observed_at')}


def create_router(principal_dependency, *, prefix='/internal/v1/workspace'):
    router = APIRouter(prefix=prefix, tags=['applications'])

    def output(app):
        result = application_out(app)
        if prefix != '/internal/v1/workspace':
            for key in ('attempt', 'release'):
                acceptance = (result.get(key) or {}).get('acceptance')
                if acceptance:
                    acceptance.pop('goal', None)
                    acceptance.pop('criteria', None)
        return result

    @router.get('/apps')
    def list_apps(principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        apps = db.scalars(select(UserApplication).where(UserApplication.environment_id == env.id)
                          .order_by(UserApplication.created_at)).all()
        databases = {row.project_path: row for row in db.scalars(select(ProjectDatabase).where(
            ProjectDatabase.environment_id == env.id, ProjectDatabase.status == 'ready'))} if apps else {}
        return {'apps': [{**output(app), 'database': release_database(app, databases.get(app.project_path))}
                         for app in apps]}

    @router.post('/apps', status_code=202)
    def publish(path: str = Query(min_length=1, max_length=255),
                x_codechat_task_id: str = Header(default='', max_length=64, pattern=r'^(?:[0-9a-f]{64})?$'),
                principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = get_settings()
        if not settings.publisher_enabled:
            raise HTTPException(503, 'Application delivery is not configured')
        try:
            WorkspaceFiles._parts(path)
        except InvalidWorkspacePath:
            raise HTTPException(400, 'Invalid project directory') from None
        env = own_environment(db, principal)
        # Fast validation only; source snapshotting belongs to the build job.
        entries = call_files(db, principal, settings, 'GET', '/v1/directories', path).json()['entries']
        if not any(entry['name'] == 'Dockerfile' and entry['kind'] == 'file' for entry in entries):
            raise HTTPException(400, 'A Dockerfile is required in this directory')
        # Serialize intent creation against the same durable environment row.
        db.refresh(env, with_for_update=True)
        database = db.scalar(select(ProjectDatabase).where(ProjectDatabase.environment_id == env.id,
            ProjectDatabase.project_path == path, ProjectDatabase.status != 'deleted'))
        if database and database.status != 'ready':
            raise HTTPException(409, 'Wait for the project database to become ready')
        database_secret = database_names(database)[2] if database else None
        objects = db.get(UserObjectStorage, env.id)
        object_secret = object_secret_name(objects) if objects and objects.status == 'ready' else None
        app = db.scalar(select(UserApplication).where(UserApplication.environment_id == env.id,
                                                      UserApplication.project_path == path))
        if app and x_codechat_task_id and app.resources.get('request_id') == x_codechat_task_id:
            return output(app)
        if app and app.status == 'changing':
            raise HTTPException(409, 'Wait for the lifecycle operation')
        if app and app.status in ('queued', 'preparing', 'building', 'deploying'):
            return output(app)
        if app and app.error_code == WRITE_REVIEW_CODE:
            raise HTTPException(409, 'Previous operation requires administrator review')
        if app and app.status == 'failed' and app.resources.get('cleanup', {}).get('status') != 'complete':
            raise HTTPException(409, 'Wait for failed release cleanup')
        if app and app.error_code and 'requires_review' in app.error_code:
            raise HTTPException(409, 'Previous operation requires administrator review')
        if app is None or app.status == 'deleted':
            active = list(db.scalars(select(UserApplication.id).where(UserApplication.environment_id == env.id,
                                                                      UserApplication.status != 'deleted')))
            if len(active) >= application_limit(db, env, settings):
                raise HTTPException(409, 'Application limit reached')
        rights = active_resources(db, env.id)
        quotas = active_quotas(db, env.id)
        builds = list(db.scalars(select(UserApplication.id).where(UserApplication.environment_id == env.id,
            UserApplication.status.in_(('queued', 'preparing', 'building', 'deploying')))))
        if len(builds) >= quotas['build_concurrency']:
            raise HTTPException(409, 'application_build_allowance_exceeded')
        admit_application(db, env, rights, exclude=app.id if app else None,
            previous=app.resources.get('last_good', {}).get('runtime_rights') if app else None)
        if app is None:
            app = UserApplication(environment_id=env.id, project_path=path, resources={'request_id': x_codechat_task_id})
            db.add(app)
        else:
            app.revision = str(uuid4())
            app.resources = {'workloads': app.resources.get('workloads', {}), 'request_id': x_codechat_task_id,
                             'last_good': app.resources.get('last_good', {})}
            app.status, app.desired_state, app.error_code = 'queued', 'running', None
        db.flush()
        app.resources = {**app.resources, 'database_secret': database_secret, 'object_storage_secret': object_secret,
                         'runtime_rights': rights, 'build_quotas': quotas,
                         'provenance': {**freeze(db, env.id, path), 'revision': app.revision}}
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                          action='application.publish', target_id=app.id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, 'Application intent changed; refresh and retry') from None
        return output(app)

    @router.post('/apps/{application_id}/{action}', status_code=202)
    def lifecycle(application_id: str, action: str, principal=Depends(principal_dependency), db=Depends(get_db)):
        if action not in ('start', 'stop', 'delete'):
            raise HTTPException(404, 'Action not found')
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        app = db.scalar(select(UserApplication).where(UserApplication.id == application_id,
                                                      UserApplication.environment_id == env.id).with_for_update())
        if not app:
            raise HTTPException(404, 'Application not found')
        if app.error_code == WRITE_REVIEW_CODE:
            raise HTTPException(409, 'Previous operation requires administrator review')
        target = {'start': 'running', 'stop': 'stopped', 'delete': 'deleted'}[action]
        if app.status == 'changing' and app.desired_state == target:
            return output(app)
        if app.status in ('queued', 'preparing', 'building', 'deploying', 'changing'):
            raise HTTPException(409, 'Wait for the current delivery task')
        if app.status == 'deleted' or not app.image and action != 'delete':
            raise HTTPException(409, 'Application is not available for this action')
        if action == 'start':
            rights = active_resources(db, env.id)
            admit_application(db, env, rights, exclude=app.id,
                previous=app.resources.get('last_good', {}).get('runtime_rights'))
            app.resources = {**app.resources, 'runtime_rights': rights}
        app.desired_state = target
        app.status = 'changing'
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                          action='application.' + action, target_id=app.id))
        db.commit()
        return output(app)

    return router
