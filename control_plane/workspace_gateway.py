"""User-bound forwarding to one recorded, TLS-protected file service."""
import ssl

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import select

from .models import AuditEvent, UserEnvironment, WorkspaceFileService, WorkspaceVolumeBinding
from .user_environments import identity_digest


def own_environment(db, principal):
    from .accounts import require_access
    require_access(db, principal)
    env = db.scalar(select(UserEnvironment).where(UserEnvironment.issuer == principal.issuer,
                                                 UserEnvironment.subject == principal.subject))
    if env is None:
        raise HTTPException(404, 'Personal space is not registered')
    if env.identity_hash != identity_digest(principal.issuer, principal.subject) or env.status == 'deleted':
        raise HTTPException(409, 'Personal space requires review')
    return env


def call_files(db, principal, settings, method: str, endpoint: str, path: str, *, body=None, overwrite=False, if_match='', params=None):
    env = own_environment(db, principal)
    if method != 'GET':
        db.refresh(env, with_for_update=True)
    binding = db.get(WorkspaceVolumeBinding, env.id)
    service = db.get(WorkspaceFileService, env.id)
    if not binding or not service or service.status != 'ready':
        raise HTTPException(409, 'Personal space is being prepared')
    namespace = 'codechat-user-' + env.identity_hash[:40]
    if binding.namespace_name != namespace:
        raise HTTPException(409, 'Personal space requires review')
    try:
        token = Fernet(settings.model_credential_encryption_key.encode()).decrypt(service.encrypted_token.encode()).decode()
        context = ssl.create_default_context(cafile=settings.workspace_service_ca)
        with httpx.Client(base_url=f'https://workspace-files.{namespace}.svc:8090', verify=context,
                          timeout=30, trust_env=False, follow_redirects=False) as client:
            response = client.request(method, endpoint, params={'path': path, 'overwrite': str(overwrite).lower(), **(params or {})},
                                      headers={'Authorization': 'Bearer ' + token, **({'If-Match': if_match} if if_match else {})}, content=body)
    except (httpx.HTTPError, InvalidToken, ValueError, OSError):
        raise HTTPException(503, 'Personal files are temporarily unavailable') from None
    if response.status_code not in (200, 201):
        code = response.status_code if response.status_code in (400, 403, 404, 409, 413, 507) else 503
        raise HTTPException(code, 'File operation could not be completed')
    if len(response.content) > (64 if endpoint == '/v1/context' else 32 if endpoint == '/v1/archive' else 16) * 1024 * 1024:
        raise HTTPException(413, 'File exceeds the transfer limit')
    action = {('GET', '/v1/archive'): 'workspace.directory.download', ('PUT', '/v1/file'): 'workspace.file.upload', ('GET', '/v1/file'): 'workspace.file.download',
              ('POST', '/v1/directories'): 'workspace.directory.create',
              ('DELETE', '/v1/file'): 'workspace.file.delete'}.get((method, endpoint))
    if action:
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                          action=action, target_id=env.id))
        db.commit()
    return response
