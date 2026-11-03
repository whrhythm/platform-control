"""Identity-bound database intents. Provisioning credentials never enter the API."""
import secrets
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
from urllib.parse import quote

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .database import get_db
from .models import AuditEvent, ProjectDatabase, UserApplication
from .settings import get_settings
from .workspace_files import WorkspaceFiles, InvalidWorkspacePath
from .workspace_gateway import own_environment, call_files

MIB = 1024 ** 2


class DatabaseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_path: str = Field(min_length=1, max_length=255)


class DatabaseDelete(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmation: str = Field(min_length=1, max_length=255)


class DatabaseOut(BaseModel):
    id: str
    project_path: str
    status: str
    error_code: str | None
    reserved_bytes: int
    used_bytes: int | None
    observed_at: datetime | None


class DatabaseList(BaseModel):
    enabled: bool
    budget_bytes: int
    reserved_bytes: int
    reservation_bytes: int
    limit: int
    quota_mode: str
    databases: list[DatabaseOut]


def public_database(row):
    return {key: getattr(row, key) for key in DatabaseOut.model_fields}


def names(row):
    suffix = UUID(row.generation).hex
    return 'ccdb_' + suffix, 'ccowner_' + suffix, 'database-' + suffix[:24]


def connection_profile(row, settings, *, rootcert='/etc/codechat-database/ca.crt'):
    database, _, _ = names(row)
    password = Fernet(settings.model_credential_encryption_key.encode()).decrypt(row.encrypted_password.encode()).decode()
    host = settings.project_database_host
    url = (f'postgresql://{database}:{quote(password, safe="")}@{host}:5432/{database}'
           f'?sslmode=verify-full&sslrootcert={quote(rootcert, safe="/")}')
    return {'DATABASE_URL': url, 'PGHOST': host, 'PGPORT': '5432', 'PGDATABASE': database,
            'PGUSER': database, 'PGPASSWORD': password, 'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': rootcert}


def create_router(principal_dependency, *, prefix='/internal/v1/workspace', agent=False):
    router = APIRouter(prefix=prefix + '/databases', tags=['project-databases'])

    def owned(db, principal, identifier):
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        row = db.scalar(select(ProjectDatabase).where(ProjectDatabase.environment_id == env.id,
                                                      ProjectDatabase.id == identifier).with_for_update())
        if row is None:
            raise HTTPException(404, 'database_not_found')
        return env, row

    def audit(db, principal, env, row, action):
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                          action='database.' + action, target_id=row.id))

    @router.get('', response_model=DatabaseList)
    def list_databases(principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings = own_environment(db, principal), get_settings()
        rows = list(db.scalars(select(ProjectDatabase).where(ProjectDatabase.environment_id == env.id)
                               .order_by(ProjectDatabase.created_at)))
        return {'enabled': settings.project_databases_enabled, 'budget_bytes': settings.project_database_budget_mib * MIB,
            'reserved_bytes': sum(row.reserved_bytes for row in rows if row.status != 'deleted'),
            'reservation_bytes': settings.project_database_reservation_mib * MIB,
            'limit': settings.project_database_limit, 'quota_mode': 'allocation_and_warning',
            'databases': [public_database(row) for row in rows]}

    @router.post('', status_code=202, response_model=DatabaseOut)
    def request_database(body: DatabaseRequest, principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = get_settings()
        if not settings.project_databases_enabled:
            raise HTTPException(503, 'project_databases_disabled')
        try:
            WorkspaceFiles._parts(body.project_path)
        except InvalidWorkspacePath:
            raise HTTPException(400, 'invalid_project_directory') from None
        env = own_environment(db, principal)
        call_files(db, principal, settings, 'GET', '/v1/directories', body.project_path)
        db.refresh(env, with_for_update=True)
        if env.status == 'suspended':
            raise HTTPException(403, 'environment_suspended')
        rows = list(db.scalars(select(ProjectDatabase).where(ProjectDatabase.environment_id == env.id)))
        row = next((value for value in rows if value.project_path == body.project_path), None)
        if row and row.status != 'deleted':
            return public_database(row)
        active = [value for value in rows if value.status != 'deleted']
        reservation = settings.project_database_reservation_mib * MIB
        if (reservation <= 0 or len(active) >= settings.project_database_limit or
                sum(value.reserved_bytes for value in active) + reservation > settings.project_database_budget_mib * MIB):
            raise HTTPException(409, 'database_allowance_exceeded')
        encrypted = Fernet(settings.model_credential_encryption_key.encode()).encrypt(secrets.token_urlsafe(36).encode()).decode()
        if row is None:
            row = ProjectDatabase(environment_id=env.id, project_path=body.project_path,
                                  encrypted_password=encrypted, reserved_bytes=reservation)
            db.add(row)
        else:
            row.generation, row.encrypted_password = str(uuid4()), encrypted
            row.status, row.error_code, row.resources = 'requested', None, {}
            row.used_bytes, row.observed_at, row.reserved_bytes = None, None, reservation
        db.flush()
        audit(db, principal, env, row, 'request')
        db.commit()
        return public_database(row)

    @router.post('/{identifier}/retry', status_code=202, response_model=DatabaseOut)
    def retry(identifier: str, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, row = owned(db, principal, identifier)
        if row.status == 'failed' and row.error_code != 'database_ownership_requires_review':
            row.status, row.error_code = ('deleting' if row.resources.get('deletion_requested') else 'requested'), None
            audit(db, principal, env, row, 'retry')
            db.commit()
        return public_database(row)

    @router.post('/{identifier}/delete', status_code=202, response_model=DatabaseOut)
    def delete(identifier: str, body: DatabaseDelete, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, row = owned(db, principal, identifier)
        if body.confirmation != row.project_path:
            raise HTTPException(400, 'database_confirmation_mismatch')
        app = db.scalar(select(UserApplication).where(UserApplication.environment_id == env.id,
            UserApplication.project_path == row.project_path, UserApplication.status != 'deleted'))
        if app:
            raise HTTPException(409, 'remove_application_before_database')
        if row.status in ('requested', 'provisioning'):
            raise HTTPException(409, 'wait_for_database_operation')
        if row.status not in ('deleted', 'deleting'):
            row.status = 'deleting'
            row.resources = {**row.resources, 'deletion_requested': True}
            audit(db, principal, env, row, 'delete')
            db.commit()
        return public_database(row)

    if agent:
        @router.get('/profile')
        def profile(response: Response, path: str = Query(min_length=1, max_length=255),
                    principal=Depends(principal_dependency), db=Depends(get_db)):
            env = own_environment(db, principal)
            row = db.scalar(select(ProjectDatabase).where(ProjectDatabase.environment_id == env.id,
                ProjectDatabase.project_path == path, ProjectDatabase.status == 'ready'))
            if row is None:
                raise HTTPException(409, 'database_not_ready')
            response.headers['Cache-Control'] = 'no-store'
            return {'environment': connection_profile(row, get_settings()),
                    'ca': Path(get_settings().project_database_ca).read_text()}

    return router
