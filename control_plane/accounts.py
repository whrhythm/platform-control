"""Admin account management through Authentik; credentials never returned or persisted."""
import hashlib
import json
import ssl
import threading
from contextlib import contextmanager
from sqlalchemy import text
from uuid import uuid4
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from .database import get_db
from .models import AccountAccess, AuditEvent
from .settings import get_settings

_local_write_lock = threading.Lock()

@contextmanager
def writing(db):
    # Separate connection keeps the advisory lock across audit/fence commits.
    engine = db.get_bind()
    if engine.dialect.name == 'postgresql':
        with engine.connect() as connection:
            if not connection.scalar(text('SELECT pg_try_advisory_lock(67021492)')):
                raise HTTPException(409, 'identity_management_busy')
            try:
                yield
            finally:
                connection.execute(text('SELECT pg_advisory_unlock(67021492)'))
    else:
        if not _local_write_lock.acquire(blocking=False):
            raise HTTPException(409, 'identity_management_busy')
        try:
            yield
        finally:
            _local_write_lock.release()

def allowed(db, issuer, subject):
    value = db.get(AccountAccess, (issuer, subject))
    return not value or not value.blocked

def require_access(db, principal):
    if not allowed(db, principal.issuer, principal.subject):
        raise HTTPException(403, 'account_disabled')

class IdentityClient:
    def __init__(self, settings):
        self.settings = settings
    def call(self, method, path, **kwargs):
        settings = self.settings
        if not settings.identity_admin_url or not settings.identity_admin_token:
            raise HTTPException(503, 'identity_management_unavailable')
        try:
            with httpx.Client(base_url=settings.identity_admin_url,
                verify=ssl.create_default_context(cafile=settings.workspace_service_ca or None),
                trust_env=False, follow_redirects=False, timeout=12) as client:
                response = client.request(method, path, headers={'Authorization':'Bearer '+settings.identity_admin_token}, **kwargs)
            if response.status_code == 404:raise HTTPException(404, 'identity_not_found')
            if response.status_code == 400:raise HTTPException(409, 'identity_conflict_or_invalid')
            if not response.is_success:raise HTTPException(503, 'identity_operation_unconfirmed')
            return response.json() if response.content else {}
        except (httpx.HTTPError, ValueError, OSError):
            raise HTTPException(503, 'identity_operation_unconfirmed') from None

class AccountInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1,max_length=150,pattern=r'^[^\s/\\]+$')
    name: str = Field(min_length=1,max_length=150)
    email: str = Field(max_length=254)
    is_active: bool = True
    revision: str = ''
    password: SecretStr | None = None
    @field_validator('email')
    @classmethod
    def email_valid(cls, value):
        if value and ('@' not in value or any(c.isspace() for c in value)):raise ValueError('invalid email')
        return value
    @field_validator('password')
    @classmethod
    def password_valid(cls,value):
        if value and not 12 <= len(value.get_secret_value()) <= 256:raise ValueError('password length')
        return value

def protected(row, principal, settings):
    return (row.get('is_superuser') or row.get('type') != 'internal' or row.get('uid') == principal.subject
            or any(g.get('name') == settings.admin_group for g in row.get('groups_obj', [])))

def revision(row):
    return hashlib.sha256(json.dumps({k:row.get(k) for k in ('uid','username','name','email','is_active','last_updated')},sort_keys=True).encode()).hexdigest()

def visible(row, principal, settings, db):
    return {**{key:row.get(key) for key in ('pk','username','name','email','uid','is_active')},
            'protected':bool(protected(row,principal,settings)), 'revision':revision(row),
            'platform_blocked':not allowed(db,settings.oidc_issuer,row['uid'])}

def record(db, principal, action, target):
    db.add(AuditEvent(tenant_id='platform',issuer=principal.issuer,subject=principal.subject,action=action,target_id=target))
    db.commit()

def fence(db, settings, subject, blocked):
    row=db.get(AccountAccess,(settings.oidc_issuer,subject))
    if row is None:
        row=AccountAccess(issuer=settings.oidc_issuer,subject=subject)
        db.add(row)
    row.blocked=blocked
    db.commit()

def router(admin_dependency):
    routes=APIRouter(prefix='/v1/admin/accounts',tags=['administration'])
    @routes.get('')
    def listing(q:str=Query('',max_length=150),page:int=Query(1,ge=1),principal=Depends(admin_dependency),db=Depends(get_db)):
        settings=get_settings()
        data=IdentityClient(settings).call('GET','core/users/',params={'search':q,'page':page,'page_size':25,'ordering':'username'})
        return {'items':[visible(row,principal,settings,db) for row in data['results']],
                'next_page':page+1 if data.get('pagination',{}).get('next') else None}
    @routes.post('')
    def create(body:AccountInput,principal=Depends(admin_dependency),db=Depends(get_db)):
        with writing(db):
            if not body.password:raise HTTPException(422,'initial_password_required')
            settings=get_settings(); client=IdentityClient(settings); operation=str(uuid4())
            record(db,principal,'identity.create.started',operation)
            try:
                row=client.call('POST','core/users/',json={**body.model_dump(include={'username','name','email'}),
                    'is_active':False,'type':'internal','path':'users/codechat','attributes':{'platform_creation':operation}})
                fence(db,settings,row['uid'],True)
                client.call('POST',f"core/users/{row['pk']}/set_password/",json={'password':body.password.get_secret_value()})
                client.call('PATCH',f"core/users/{row['pk']}/",json={'is_active':body.is_active})
                row=client.call('GET',f"core/users/{row['pk']}/")
                if row['is_active'] != body.is_active:raise HTTPException(409,'identity_operation_unconfirmed')
                fence(db,settings,row['uid'],not row['is_active'])
            except HTTPException:
                record(db,principal,'identity.create.requires_review',operation)
                raise
            record(db,principal,'identity.created',str(row['pk']))
            return visible(row,principal,settings,db)
    @routes.put('/{identifier}')
    def update(identifier:int,body:AccountInput,principal=Depends(admin_dependency),db=Depends(get_db)):
        with writing(db):
            settings=get_settings();client=IdentityClient(settings)
            row=client.call('GET',f'core/users/{identifier}/')
            if protected(row,principal,settings):raise HTTPException(403,'protected_identity')
            if body.revision!=revision(row):raise HTTPException(409,'identity_revision_changed')
            # Account disable denies platform requests before the remote write; uncertain enable stays blocked.
            fence(db,settings,row['uid'],True)
            record(db,principal,'identity.update.started',str(identifier))
            try:
                if body.password:client.call('POST',f'core/users/{identifier}/set_password/',json={'password':body.password.get_secret_value()})
                data=body.model_dump(include={'username','name','email','is_active'})
                client.call('PATCH',f'core/users/{identifier}/',json=data)
                actual=client.call('GET',f'core/users/{identifier}/')
                if any(actual.get(k)!=v for k,v in data.items()):raise HTTPException(409,'identity_operation_unconfirmed')
                fence(db,settings,actual['uid'],not actual['is_active'])
            except HTTPException:
                record(db,principal,'identity.update.requires_review',str(identifier))
                raise
            record(db,principal,'identity.updated',str(identifier))
            return visible(actual,principal,settings,db)
    return routes
