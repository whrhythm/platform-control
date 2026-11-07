"""Server-only sponsored model calls; ambiguous results retain their budget reservation."""
import anyio
import hashlib
import json
from datetime import timezone
from decimal import Decimal, ROUND_CEILING
import httpx
import jwt
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .ai_releases import entry_access, enabled
from .auth import Principal
from .database import get_db
from .models import AIApplication, AIAppRun, AIAppCall, AIConfiguration, ModelCredential, now

router = APIRouter(prefix='/agent/app/v1', tags=['ai-sponsor'])


def rates(db, model):
    catalog = db.get(AIConfiguration, 'default')
    price = next((m for m in catalog.catalog if m.get('name') == model), {}) if catalog else {}
    try:
        values = tuple(Decimal(str(price[k])) for k in ('input_cost_per_token', 'output_cost_per_token'))
        if any(not v.is_finite() or v <= 0 or v > 1 for v in values):
            raise ValueError()
        return values
    except Exception:
        raise HTTPException(503, 'ai_releases_price_unavailable') from None


def amount(prompt, completion, input_rate, output_rate):
    return int(((prompt*Decimal(input_rate)+completion*Decimal(output_rate))*1000000).to_integral_value(rounding=ROUND_CEILING))


def admission(db, claims, body, settings):
    run = db.get(AIAppRun, claims['run'])
    if not run or run.entry_id != claims['entry']:
        raise HTTPException(403, 'ai_releases_access_denied')
    principal = Principal(claims['iss'], claims['sub'])
    entry, release, _, snap, actor, sponsor, app, _ = entry_access(db, principal, run.entry_id)
    # One lock order for admission, publication, member changes and budget changes.
    db.refresh(sponsor, with_for_update=True)
    db.refresh(app, with_for_update=True)
    db.expire_all()
    entry_access(db, principal, run.entry_id)
    if body.get('model') != snap.model.model or body.get('stream') is not True:
        raise HTTPException(400, 'ai_releases_invalid_request')
    messages = body.get('messages')
    if not isinstance(messages, list) or not messages or len(messages) > 100:
        raise HTTPException(400, 'ai_releases_invalid_request')
    tools = body.get('tools', [])
    if not isinstance(tools, list):
        raise HTTPException(400, 'ai_releases_invalid_request')
    for tool in tools:
        if (not isinstance(tool, dict) or tool.get('type') != 'function' or
                not isinstance(tool.get('function'), dict) or
                tool['function'].get('name') != 'file_search' or not snap.knowledge):
            raise HTTPException(400, 'ai_releases_invalid_request')
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if db.scalar(select(AIAppCall.id).where(AIAppCall.run_id == run.id, AIAppCall.request_hash == digest)):
        raise HTTPException(409, 'ai_releases_call_already_submitted')
    if db.scalar(select(func.count()).select_from(AIAppCall).where(AIAppCall.run_id == run.id)) >= settings.ai_releases_steps:
        raise HTTPException(409, 'ai_releases_step_limit')
    input_rate, output_rate = rates(db, snap.model.model)
    # UTF-8 bytes plus per-message/tool framing bound token input conservatively.
    prompt_bound = len(json.dumps({'messages':messages,'tools':body.get('tools',[])}, ensure_ascii=False).encode())+1024+len(messages)*32
    if prompt_bound > settings.ai_releases_input_bytes:
        raise HTTPException(413, 'ai_releases_context_limit')
    reserve = amount(prompt_bound, snap.model.max_output_tokens, input_rate, output_rate)
    held = db.scalar(select(func.coalesce(func.sum(AIAppCall.held_micros), 0)).where(AIAppCall.project_id == app.project_id))
    if held+reserve > app.budget_micros:
        raise HTTPException(429, 'ai_releases_budget_exhausted')
    credential = db.scalar(select(ModelCredential).where(ModelCredential.issuer == sponsor.issuer,
        ModelCredential.subject == sponsor.subject, ModelCredential.status == 'ready'))
    if not credential:
        raise HTTPException(503, 'ai_releases_sponsor_unavailable')
    key = Fernet(settings.model_credential_encryption_key.encode()).decrypt(credential.encrypted_key.encode()).decode()
    call = AIAppCall(project_id=app.project_id, run_id=run.id, request_hash=digest,
        reserved_micros=reserve, held_micros=reserve, input_rate=str(input_rate), output_rate=str(output_rate))
    db.add(call)
    db.flush()
    payload = {k:body[k] for k in ('messages','tools','tool_choice') if k in body}
    payload.update(model=snap.model.model, temperature=snap.model.temperature, max_tokens=snap.model.max_output_tokens,
        stream=True, stream_options={'include_usage':True}, metadata={'actor_id':actor.identity_hash,
        'sponsor_id':sponsor.identity_hash, 'project_id':app.project_id, 'release_id':release.id,
        'app_call_id':call.id, 'purpose':'published_application'})
    # LiteLLM retains custom attribution only inside its supported spend log field.
    payload['metadata']['spend_logs_metadata'] = dict(payload['metadata'])
    identifier, engine = call.id, db.get_bind()
    db.commit()  # Never hold quota locks during provider I/O.
    return identifier, engine, key, payload


def settle(engine, identifier, usage, *, completed=False, rejected=False):
    with Session(engine) as db:
        call = db.get(AIAppCall, identifier, with_for_update=True)
        if call.state != 'reserved':
            return
        if rejected:
            call.state, call.held_micros = 'rejected', 0
        elif (completed and isinstance(usage, dict) and
              all(type(usage.get(k)) is int and 0 <= usage[k] <= 1000000 for k in ('prompt_tokens','completion_tokens'))):
            cost = amount(usage['prompt_tokens'], usage['completion_tokens'], call.input_rate, call.output_rate)
            call.prompt_tokens, call.completion_tokens = usage['prompt_tokens'], usage['completion_tokens']
            # An unexpected provider overrun must remain visible rather than silently discounted.
            call.held_micros, call.state = cost, 'completed' if cost <= call.reserved_micros else 'uncertain'
        else:
            call.state = 'uncertain'
        db.commit()


@router.post('/chat/completions')
async def completion(request: Request, authorization: str = Header(default=''), db=Depends(get_db)):
    settings = enabled()
    if not authorization.startswith('Bearer sbapp_'):
        raise HTTPException(403, 'ai_releases_access_denied')
    try:
        claims = jwt.decode(authorization[len('Bearer sbapp_'):], settings.librechat_service_token,
            algorithms=['HS256'], audience='ai-application', issuer=settings.oidc_issuer,
            options={'require':['exp','iss','sub','run','entry']})
    except jwt.PyJWTError:
        raise HTTPException(403, 'ai_releases_access_denied') from None
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 512*1024:
            raise HTTPException(413, 'ai_releases_context_limit')
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
    except ValueError:
        raise HTTPException(400, 'ai_releases_invalid_request') from None
    try:
        identifier, engine, key, payload = admission(db, claims, body, settings)
    except HTTPException as exc:
        if exc.detail == 'ai_releases_budget_exhausted':
            # A hard application budget is not transient provider rate limiting.
            return JSONResponse(status_code=402, headers={'x-should-retry':'false'}, content={
                'error':{'code':exc.detail, 'type':'insufficient_quota',
                    'message':'应用预算不足。请联系创建者调整预算或补充模型额度后再试；本次未发起模型调用。'}})
        raise
    remaining = max(0.1, claims['exp']-now().timestamp())
    client = httpx.AsyncClient(timeout=httpx.Timeout(remaining, connect=min(10,remaining)), trust_env=False)
    try:
        upstream = await client.send(client.build_request('POST', settings.litellm_url.rstrip('/')+'/v1/chat/completions',
            json=payload, headers={'Authorization':'Bearer '+key, 'x-litellm-call-id':identifier}), stream=True)
        if upstream.status_code != 200:
            status = upstream.status_code
            await upstream.aclose()
            settle(engine, identifier, None, rejected=status in (400,401,403,422,429))
            await client.aclose()
            raise HTTPException(429 if status==429 else 503, 'ai_releases_model_unavailable')
    except httpx.HTTPError:
        settle(engine, identifier, None)
        await client.aclose()
        raise HTTPException(503, 'ai_releases_result_unknown') from None

    async def stream():
        usage, complete = None, False
        try:
            with anyio.fail_after(max(0.1, claims['exp']-now().timestamp())):
                async for line in upstream.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        complete = True
                        yield 'data: [DONE]\n\n'
                        break
                    try:
                        event = json.loads(data)
                    except ValueError:
                        raise RuntimeError('invalid_model_stream') from None
                    if event.get('usage'):
                        usage = event['usage']
                    # Provider metadata, credentials and internal reasoning are not relayed.
                    clean = {k:event[k] for k in ('id','object','created','model','choices','usage') if k in event}
                    for choice in clean.get('choices', []):
                        delta = choice.get('delta', {})
                        choice['delta'] = {k:delta[k] for k in ('role','content','tool_calls') if k in delta}
                    yield 'data: '+json.dumps(clean, ensure_ascii=False)+'\n\n'
                if not complete:
                    raise RuntimeError('incomplete_model_stream')
        except (httpx.HTTPError, TimeoutError, RuntimeError):
            yield 'data: '+json.dumps({'error':{'message':'应用回答中断，已收到的内容保留。请核对后手动重试；本次费用仍待核验。','code':'ai_releases_result_unknown'}})+'\n\n'
        finally:
            settle(engine, identifier, usage, completed=complete)
            await upstream.aclose()
            await client.aclose()
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
