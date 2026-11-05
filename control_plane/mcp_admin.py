import asyncio
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import IntegrityError

from .database import get_db
from .models import MCPServer, MCPCredential, AuditEvent, now
from .settings import get_settings
from .mcp_catalog import ServerInput, servers, server, encrypt, decrypt, credential_value, safe_schema, published
from .mcp_client import gateway


class Revision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)


class Probe(Revision):
    secret: SecretStr | None = None


class Review(Revision):
    tools: dict[str, str] = Field(max_length=40)
    acknowledged: bool


class CredentialInput(Revision):
    secret: SecretStr | None = None  # null revokes; never returned.


def visible(row):
    return {'id': row.id, 'revision': row.revision, 'state': row.state, **row.config,
            'discovered': row.discovered, 'approved': row.approved, 'addresses': row.addresses,
            'checked_at': row.checked_at, 'has_credential': bool(row.encrypted_credential)}


def commit(db, principal, action, target):
    db.add(AuditEvent(tenant_id='platform', issuer=principal.issuer, subject=principal.subject,
                     action=action, target_id=target))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'mcp_configuration_changed') from None


def locked(db, identifier, revision):
    row = db.get(MCPServer, identifier, with_for_update=True)
    if row is None and identifier == 'deepwiki':
        row = server(db, identifier)
        db.add(row)
    if row is None:
        raise HTTPException(404, 'mcp_server_not_found')
    if row.revision != revision:
        raise HTTPException(409, 'mcp_configuration_changed')
    return row


def admin_router(admin_dependency):
    router = APIRouter(prefix='/v1/admin/mcp', tags=['administration'])

    @router.get('')
    def listing(principal=Depends(admin_dependency), db=Depends(get_db)):
        return {'servers': [visible(row) for row in servers(db)]}

    def save(body, row, settings):
        config = body.model_dump(exclude={'revision', 'secret'})
        existing = row.config or {}
        if body.auth == 'platform_bearer':
            if body.secret is not None:
                row.encrypted_credential = encrypt(body.secret.get_secret_value(), settings, 'platform', row.id, 0)
            elif not row.encrypted_credential or existing.get('url') != config['url'] or existing.get('auth') != body.auth:
                raise HTTPException(422, 'mcp_platform_credential_required')
        else:
            if body.secret is not None:
                raise HTTPException(422, 'mcp_credential_not_applicable')
            row.encrypted_credential = None
        row.config, row.state = config, 'draft'
        row.discovered, row.approved, row.addresses, row.checked_at = [], [], [], None

    @router.post('')
    def create(body: ServerInput, principal=Depends(admin_dependency), db=Depends(get_db)):
        if body.revision != 0 or len(servers(db)) >= 30:
            raise HTTPException(409, 'mcp_catalog_limit')
        row = MCPServer(id='mcp-' + uuid4().hex, revision=1)
        save(body, row, get_settings())
        db.add(row)
        commit(db, principal, 'mcp.server.created', row.id)
        return visible(row)

    @router.put('/{identifier}')
    def update(identifier: str, body: ServerInput, principal=Depends(admin_dependency), db=Depends(get_db)):
        row = locked(db, identifier, body.revision)
        save(body, row, get_settings())
        row.revision += 1
        commit(db, principal, 'mcp.server.updated', row.id)
        return visible(row)

    @router.post('/{identifier}/probe')
    def probe(identifier: str, body: Probe, principal=Depends(admin_dependency), db=Depends(get_db)):
        row = locked(db, identifier, body.revision)
        settings = get_settings()
        secret = ''
        if row.config['auth'] == 'platform_bearer':
            secret = decrypt(row.encrypted_credential, settings, 'platform', row.id, 0)
        elif row.config['auth'] == 'user_bearer':
            if body.secret is None:
                raise HTTPException(422, 'mcp_probe_credential_required')
            secret = credential_value(body.secret.get_secret_value())
        # A probe revokes the old reviewed version before networking, even on failure.
        row.state, row.discovered, row.approved, row.addresses = 'draft', [], [], []
        row.revision += 1
        revision, config = row.revision, dict(row.config)
        commit(db, principal, 'mcp.server.probe.started', row.id)
        try:
            result = asyncio.run(gateway(settings, 'discover', config, secret=secret))
            if 'error' in result:
                raise ValueError('unavailable')
            tools, addresses = result['tools'], result['addresses']
            if not tools or len(tools) > 80 or len({t['name'] for t in tools}) != len(tools):
                raise ValueError('invalid_tools')
            if any(not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', t['name']) for t in tools):
                raise ValueError('invalid_tools')
        except Exception:
            commit(db, principal, 'mcp.server.probe.failed', identifier)
            raise HTTPException(503, 'mcp_probe_failed') from None
        db.expire_all()
        row = locked(db, identifier, revision)
        row.state, row.discovered, row.addresses, row.checked_at = 'verified', tools, addresses, now()
        row.revision += 1
        commit(db, principal, 'mcp.server.verified', row.id)
        return visible(row)

    @router.post('/{identifier}/publish')
    def publish(identifier: str, body: Review, principal=Depends(admin_dependency), db=Depends(get_db)):
        row = locked(db, identifier, body.revision)
        discovered = {tool['name']: tool for tool in row.discovered}
        if (row.state != 'verified' or not body.acknowledged or not body.tools
                or not body.tools.keys() <= discovered.keys() or not set(body.tools.values()) <= {'read', 'write'}):
            raise HTTPException(409, 'mcp_review_required')
        # Shared credentials must never authorize writes on behalf of every user.
        if row.config['auth'] == 'platform_bearer' and 'write' in body.tools.values():
            raise HTTPException(422, 'mcp_shared_credential_read_only')
        if any(not discovered[name].get('supported') or not safe_schema(discovered[name]['inputSchema']) for name in body.tools):
            raise HTTPException(422, 'mcp_schema_unsupported')
        row.approved = [{**discovered[name], 'permission': permission} for name, permission in body.tools.items()]
        row.state = 'published'
        row.revision += 1
        commit(db, principal, 'mcp.server.published', row.id)
        return visible(row)

    @router.post('/{identifier}/{action}')
    def disable(identifier: str, action: str, body: Revision, principal=Depends(admin_dependency), db=Depends(get_db)):
        if action not in ('disable', 'archive'):
            raise HTTPException(404, 'mcp_action_unknown')
        row = locked(db, identifier, body.revision)
        row.state = 'disabled' if action == 'disable' else 'archived'
        row.revision += 1
        if action == 'archive':
            row.encrypted_credential = None
            from sqlalchemy import select
            for credential in db.scalars(select(MCPCredential).where(MCPCredential.server_id == identifier)):
                credential.encrypted_value = None
                credential.revision += 1
        commit(db, principal, 'mcp.server.' + action, row.id)
        return visible(row)

    return router


def credential_router(principal_dependency):
    from .workspace_gateway import own_environment
    router = APIRouter(prefix='/internal/v1/workspace/agent', tags=['programming-workbench'])

    @router.put('/mcp/{identifier}/credential')
    def save_credential(identifier: str, body: CredentialInput, principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        if env.status == 'suspended':
            raise HTTPException(403, 'agent_account_suspended')
        db.refresh(env, with_for_update=True)
        row = db.get(MCPServer, identifier, with_for_update=True)
        if not row or row.config['auth'] != 'user_bearer' or (body.secret is not None and not published(db, get_settings(), row)):
            raise HTTPException(404, 'mcp_server_not_found')
        credential = db.get(MCPCredential, (env.id, identifier))
        if (credential.revision if credential else 0) != body.revision:
            raise HTTPException(409, 'mcp_credential_changed')
        if not credential:
            credential = MCPCredential(environment_id=env.id, server_id=identifier, revision=0)
            db.add(credential)
        credential.revision += 1
        credential.server_revision = row.revision
        credential.encrypted_value = (encrypt(body.secret.get_secret_value(), get_settings(), env.id, identifier, credential.revision)
                                      if body.secret is not None else None)
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                         action='mcp.credential.updated' if body.secret else 'mcp.credential.revoked', target_id=identifier))
        db.commit()
        return {'configured': bool(credential.encrypted_value), 'revision': credential.revision}

    return router
