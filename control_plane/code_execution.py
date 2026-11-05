"""Metered CodeAPI gateway. Only verified LibreChat identities can reserve compute."""
import hashlib
import json
import re
from datetime import timedelta, timezone
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select

from .auth import Principal
from .database import get_db
from .models import CapacityAccount, CodeExecution, now
from .settings import get_settings
from .workspace_gateway import own_environment

FIELDS = ('code_runs_monthly', 'code_timeout_seconds', 'code_concurrency')


def code_rights(plan):
    defaults = {'basic': (300, 15, 1), 'pro': (1500, 15, 2)}.get(plan.get('id'), (30, 10, 1))
    return {key: plan.get(key) if plan.get(key) is not None else value for key, value in zip(FIELDS, defaults)}


def month_key():
    return now().astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m')


def usage(db, env):
    account = db.get(CapacityAccount, env.id)
    rights = code_rights(account.plan if account else {})
    count = db.scalar(select(func.count()).select_from(CodeExecution).where(
        CodeExecution.environment_id == env.id, CodeExecution.period == month_key(),
        CodeExecution.operation == 'exec', CodeExecution.state != 'rejected'))
    active = db.scalar(select(func.count()).select_from(CodeExecution).where(
        CodeExecution.environment_id == env.id, CodeExecution.state.in_(('running', 'unknown')), CodeExecution.lease_until > now()))
    return {**rights, 'period': month_key(), 'used_runs': count, 'active_runs': active,
            'remaining_runs': max(0, rights['code_runs_monthly'] - count),
            'memory_mib': 256, 'accounting': 'dispatched_runs', 'timezone': 'Asia/Shanghai'}


def authenticate(token, settings):
    if not settings.code_execution_enabled or not settings.code_execution_public_key:
        raise HTTPException(503, 'code_execution_unavailable')
    try:
        claims = jwt.decode(token, settings.code_execution_public_key, algorithms=['EdDSA'],
                            issuer='librechat', audience='codeapi',
                            options={'require': ['exp', 'iat', 'sub', 'platform_subject', 'platform_issuer']})
        if (claims['platform_issuer'].rstrip('/') != settings.oidc_issuer.rstrip('/') or
                not isinstance(claims['platform_subject'], str) or not 1 <= len(claims['platform_subject']) <= 255 or
                claims['exp'] - claims['iat'] > 300):
            raise ValueError()
        return Principal(settings.oidc_issuer, claims['platform_subject'])
    except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError):
        raise HTTPException(403, 'code_execution_identity_required') from None


def reserve(db, principal, key, digest, operation='exec', transfer_bytes=0):
    env = own_environment(db, principal)
    # The existing environment row serializes admission across API replicas.
    db.refresh(env, with_for_update=True)
    if env.status in ('suspended', 'deleted'):
        raise HTTPException(403, 'code_execution_account_disabled')
    existing = db.scalar(select(CodeExecution).where(CodeExecution.environment_id == env.id, CodeExecution.request_key == key))
    if existing:
        raise HTTPException(409, 'code_execution_already_submitted')
    state = usage(db, env)
    if state['remaining_runs'] < 1:
        raise HTTPException(429, 'code_execution_quota_exhausted')
    if operation == 'upload':
        uploads, transferred = db.execute(select(func.count(), func.coalesce(func.sum(CodeExecution.transfer_bytes), 0)).where(
            CodeExecution.environment_id == env.id, CodeExecution.period == state['period'],
            CodeExecution.operation == 'upload', CodeExecution.state != 'rejected')).one()
        if uploads >= state['code_runs_monthly'] * 4 or transferred + transfer_bytes > state['code_runs_monthly'] * 16 * 1024 * 1024:
            raise HTTPException(429, 'code_execution_upload_quota_exhausted')
    if state['active_runs'] >= state['code_concurrency']:
        raise HTTPException(429, 'code_execution_busy')
    row = CodeExecution(environment_id=env.id, request_key=key, request_hash=digest,
                        operation=operation, transfer_bytes=transfer_bytes,
                        period=state['period'], state='running', timeout_seconds=state['code_timeout_seconds'],
                        lease_until=now() + timedelta(seconds=get_settings().code_execution_lease_seconds))
    db.add(row)
    db.commit()
    return row


router = APIRouter(prefix='/internal/v1/codeapi', tags=['code-execution'])


@router.get('/{path:path}', operation_id='codeapi_read')
@router.post('/{path:path}', operation_id='codeapi_submit')
@router.delete('/{path:path}', operation_id='codeapi_delete')
async def gateway(path: str, request: Request, db=Depends(get_db)):
    settings = get_settings()
    authorization = request.headers.get('authorization', '')
    principal = authenticate(authorization.removeprefix('Bearer '), settings)
    env = own_environment(db, principal)
    if env.status == 'suspended':
        raise HTTPException(403, 'code_execution_account_disabled')
    # No arbitrary proxy, stateful command, hosted app or programmatic bypass.
    valid = ((request.method == 'POST' and path in ('exec', 'upload', 'upload/batch')) or
             (request.method == 'GET' and re.fullmatch(r'(download/[\w-]+/[\w-]+|files/[\w-]+|sessions/[\w-]+/objects/[\w-]+)', path)) or
             (request.method == 'DELETE' and re.fullmatch(r'(files/[\w-]+/[\w-]+|sessions/[\w-]+/objects/[\w-]+)', path)))
    if not valid:
        raise HTTPException(404, 'code_execution_route_unavailable')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.code_execution_transfer_bytes:
            raise HTTPException(413, 'code_execution_file_too_large')
    row = None
    if path == 'exec':
        try:
            value = json.loads(body)
            if not isinstance(value, dict) or not isinstance(value.get('code'), str) or not value['code'] or not isinstance(value.get('lang'), str):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, 'code_execution_invalid_request') from None
        key = request.headers.get('idempotency-key') or str(uuid4())
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', key):
            raise HTTPException(400, 'code_execution_invalid_request')
        row = reserve(db, principal, key, hashlib.sha256(body).hexdigest())
        # Never accept a user-supplied larger timeout or alternate runtime route.
        value['timeout'] = row.timeout_seconds * 1000
        value.pop('runtime_session_hint', None)
        body = json.dumps(value).encode()
    elif request.method == 'POST':
        row = reserve(db, principal, str(uuid4()), hashlib.sha256(body).hexdigest(), 'upload', len(body))
    db.commit()  # Do not hold a transaction or lock while waiting on execution.
    try:
        async with httpx.AsyncClient(base_url=settings.code_execution_url, timeout=settings.code_execution_timeout_seconds,
                                     follow_redirects=False, trust_env=False) as client:
            async with client.stream(request.method, path, content=bytes(body),
                                     params=request.query_params,
                                     headers={'Authorization': authorization,
                                              'Content-Type': request.headers.get('content-type', 'application/json')}) as upstream:
                content = bytearray()
                async for chunk in upstream.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > settings.code_execution_transfer_bytes:
                        raise HTTPException(413, 'code_execution_file_too_large')
                status = upstream.status_code
                media = upstream.headers.get('content-type', 'application/octet-stream')
        if row:
            row.state = 'complete' if status < 300 else 'rejected' if status in (400, 401, 403, 413, 429) else 'unknown'
            row.completed_at = now()
            db.commit()
        if status >= 400:
            raise HTTPException(status if status in (400, 403, 404, 409, 413, 429) else 503, 'code_execution_provider_failed')
        return Response(bytes(content), status_code=status, headers={'Content-Type': media, 'Cache-Control': 'no-store'})
    except HTTPException:
        if row and row.state == 'running':
            row.state = 'unknown'
            db.commit()
        raise
    except httpx.HTTPError:
        if row and row.state == 'running':
            row.state = 'unknown'
            db.commit()
        # Unclear submissions remain charged. Never retry a code execution automatically.
        raise HTTPException(503, 'code_execution_result_unconfirmed') from None
