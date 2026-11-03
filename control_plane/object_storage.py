"""Public object-storage state and owner-only runtime connection profiles."""
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select

from .database import get_db
from .models import AuditEvent, StorageMeasurement, UserObjectStorage
from .settings import get_settings
from .workspace_gateway import own_environment, call_files


class ObjectStorageOut(BaseModel):
    enabled: bool
    status: str
    bucket: str | None
    budget_bytes: int | None
    error_code: str | None


def storage_out(row, settings):
    return {'enabled': settings.object_storage_enabled, 'status': row.status if row else 'not_prepared',
            'bucket': row.bucket_name if row else None,
            'budget_bytes': row.resources.get('budget_bytes') if row else None,
            'error_code': row.error_code if row else None}


def secret_name(row):
    from uuid import UUID
    return 'objects-' + UUID(row.generation).hex[:24]


def connection_profile(row, settings):
    if row.resources.get('endpoint') != settings.object_storage_endpoint:
        raise HTTPException(409, 'object_storage_endpoint_changed')
    secret = Fernet(settings.model_credential_encryption_key.encode()).decrypt(row.encrypted_secret.encode()).decode()
    return {'S3_ENDPOINT_URL': ('https://' if settings.object_storage_secure else 'http://') + settings.object_storage_endpoint,
            'S3_BUCKET': row.bucket_name, 'AWS_ACCESS_KEY_ID': row.access_key, 'AWS_SECRET_ACCESS_KEY': secret,
            'AWS_DEFAULT_REGION': 'us-east-1', 'AWS_REGION': 'us-east-1'}


def create_router(principal_dependency, *, prefix='/internal/v1/workspace', agent=False):
    router = APIRouter(prefix=prefix + '/object-storage', tags=['object-storage'])

    @router.get('', response_model=ObjectStorageOut)
    def state(principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        return storage_out(db.get(UserObjectStorage, env.id), get_settings())

    @router.post('/retry', response_model=ObjectStorageOut, status_code=202)
    def retry(principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        if env.status == 'suspended':
            raise HTTPException(403, 'object_storage_suspended')
        row = db.get(UserObjectStorage, env.id)
        if not row:
            raise HTTPException(409, 'object_storage_preparing')
        if row.status == 'failed':
            row.status, row.error_code = 'preparing', None
            db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                             action='object_storage.retry', target_id=row.generation))
            db.commit()
        return storage_out(row, get_settings())

    if agent:
        @router.get('/profile')
        def profile(response: Response, path: str = Query(min_length=1, max_length=255),
                    principal=Depends(principal_dependency), db=Depends(get_db)):
            env = own_environment(db, principal)
            row = db.get(UserObjectStorage, env.id)
            if not row or row.status != 'ready':
                raise HTTPException(409, 'object_storage_not_ready')
            call_files(db, principal, get_settings(), 'GET', '/v1/directories', path)
            response.headers['Cache-Control'] = 'no-store'
            return {'environment': {**connection_profile(row, get_settings()), 'CODECHAT_OBJECT_PREFIX': path + '/'}}

    return router
