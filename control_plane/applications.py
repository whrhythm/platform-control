"""User-facing application intents; provider writes belong to the controller."""
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .database import get_db
from .models import AuditEvent, UserApplication
from .settings import get_settings
from .workspace_gateway import own_environment, call_files
from .workspace_files import WorkspaceFiles, InvalidWorkspacePath


def application_out(app):
    result = {key: getattr(app, key) for key in ('id', 'project_path', 'status', 'desired_state', 'image', 'url', 'error_code', 'observation')}
    result['cleanup'] = app.resources.get('cleanup')
    result['previous_url'] = app.resources.get('last_good', {}).get('url') if app.status == 'failed' and app.resources.get('cleanup', {}).get('runtime') == 'previous_release' else None
    return result


def create_router(principal_dependency, *, prefix='/internal/v1/workspace'):
    router = APIRouter(prefix=prefix, tags=['applications'])

    @router.get('/apps')
    def list_apps(principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        return {'apps': [application_out(app) for app in db.scalars(select(UserApplication)
            .where(UserApplication.environment_id == env.id).order_by(UserApplication.created_at))]}

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
        app = db.scalar(select(UserApplication).where(UserApplication.environment_id == env.id,
                                                      UserApplication.project_path == path))
        if app and x_codechat_task_id and app.resources.get('request_id') == x_codechat_task_id:
            return application_out(app)
        if app and app.status == 'changing':
            raise HTTPException(409, 'Wait for the lifecycle operation')
        if app and app.status in ('queued', 'preparing', 'building', 'deploying'):
            return application_out(app)
        if app and app.status == 'failed' and app.resources.get('cleanup', {}).get('status') != 'complete':
            raise HTTPException(409, 'Wait for failed release cleanup')
        if app and app.error_code and 'requires_review' in app.error_code:
            raise HTTPException(409, 'Previous operation requires administrator review')
        if app is None:
            active = list(db.scalars(select(UserApplication.id).where(UserApplication.environment_id == env.id,
                                                                      UserApplication.status != 'deleted')))
            if len(active) >= settings.default_application_limit:
                raise HTTPException(409, 'Application limit reached')
            app = UserApplication(environment_id=env.id, project_path=path, resources={'request_id': x_codechat_task_id})
            db.add(app)
        else:
            app.revision = str(uuid4())
            app.resources = {'workloads': app.resources.get('workloads', {}), 'request_id': x_codechat_task_id,
                             'last_good': app.resources.get('last_good', {})}
            app.status, app.desired_state, app.error_code = 'queued', 'running', None
        db.flush()
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                          action='application.publish', target_id=app.id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, 'Application intent changed; refresh and retry') from None
        return application_out(app)

    @router.post('/apps/{application_id}/{action}', status_code=202)
    def lifecycle(application_id: str, action: str, principal=Depends(principal_dependency), db=Depends(get_db)):
        if action not in ('start', 'stop', 'delete'):
            raise HTTPException(404, 'Action not found')
        env = own_environment(db, principal)
        app = db.scalar(select(UserApplication).where(UserApplication.id == application_id,
                                                      UserApplication.environment_id == env.id).with_for_update())
        if not app:
            raise HTTPException(404, 'Application not found')
        target = {'start': 'running', 'stop': 'stopped', 'delete': 'deleted'}[action]
        if app.status == 'changing' and app.desired_state == target:
            return application_out(app)
        if app.status in ('queued', 'preparing', 'building', 'deploying', 'changing'):
            raise HTTPException(409, 'Wait for the current delivery task')
        if app.status == 'deleted' or not app.image and action != 'delete':
            raise HTTPException(409, 'Application is not available for this action')
        app.desired_state = target
        app.status = 'changing'
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                          action='application.' + action, target_id=app.id))
        db.commit()
        return application_out(app)

    return router
