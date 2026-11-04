"""Owner-bound task ledger. Commands/prompts are encrypted and never sent to logs."""
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from .database import get_db
from .models import AgentPreference, AgentTask, AuditEvent, UserEnvironment, now
from .settings import get_settings
from .workspace_files import WorkspaceFiles
from .workspace_gateway import call_files, own_environment

ACTIVE = ('queued', 'running', 'awaiting_approval', 'cancelling')
DEFAULT = {'project_path': '', 'strategy': 'implement', 'approval': 'strict'}


def conversation_key(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', value) else 'default'


def seal(value, settings):
    return Fernet(settings.model_credential_encryption_key.encode()).encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()


def unseal(value, settings):
    return json.loads(Fernet(settings.model_credential_encryption_key.encode()).decrypt(value.encode()))


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_path: str = Field(default='', max_length=255)
    strategy: Literal['implement', 'plan', 'review'] = 'implement'
    approval: Literal['strict', 'workspace_auto'] = 'strict'

    @field_validator('project_path')
    @classmethod
    def valid_path(cls, value):
        if value:
            WorkspaceFiles._parts(value)
        return value


class PreferenceInput(AgentConfig):
    conversation_id: str = Field(default='default', pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    revision: int = Field(ge=0)


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(pattern=r'^[0-9a-f-]{36}$')
    decision: Literal['approve', 'reject']


def preference(db, environment_id, conversation_id):
    row = db.get(AgentPreference, (environment_id, conversation_id))
    fallback = db.get(AgentPreference, (environment_id, 'default')) if row is None else None
    return {'conversation_id': conversation_id, 'revision': row.revision if row else 0,
            **(row.config if row else fallback.config if fallback else DEFAULT)}


def append_event(db, task, settings, kind, **payload):
    events = unseal(task.encrypted_events, settings)
    if len(events) >= 250 and kind != 'finished':
        raise RuntimeError('agent_event_limit')
    events.append({'sequence': len(events) + 1, 'kind': kind, 'at': now().isoformat(), **payload})
    task.encrypted_events, task.updated_at = seal(events, settings), now()


def task_out(task, settings):
    events = unseal(task.encrypted_events, settings)
    for event in events:
        if event.get('operation_id') != (task.pending or {}).get('id'):
            event.pop('arguments', None)
        if event.get('output'):
            event['output'] = event['output'][-8192:]
    return {'id': task.id, 'conversation_id': task.conversation_id, 'config': task.config,
        'status': task.status, 'error_code': task.error_code, 'created_at': task.created_at,
        'updated_at': task.updated_at, 'events': events,
        'pending': task.pending, 'cleanup': task.resources.get('cleanup')}


def submit_task(db, principal, settings, conversation_id, prompt, model, message_id=''):
    env = own_environment(db, principal)
    if env.status == 'suspended':
        raise HTTPException(403, 'agent_account_suspended')
    key = conversation_key(conversation_id)
    config = preference(db, env.id, key)
    if not config['project_path']:
        raise HTTPException(409, 'agent_project_required')
    call_files(db, principal, settings, 'GET', '/v1/directories', config['project_path'])
    db.refresh(env, with_for_update=True)
    db.expire_all()
    if config != preference(db, env.id, key):
        raise HTTPException(409, 'agent_settings_changed')
    config = {**config, 'model': model}
    request_key = hashlib.sha256((key + '\n' + message_id + '\n' + prompt + '\n' + json.dumps(config, sort_keys=True)).encode()).hexdigest()
    previous = db.scalar(select(AgentTask).where(AgentTask.environment_id == env.id, AgentTask.request_key == request_key))
    if previous:
        return previous
    if db.scalar(select(AgentTask.id).where(AgentTask.environment_id == env.id, AgentTask.status.in_(ACTIVE))):
        raise HTTPException(409, 'agent_task_active')
    task = AgentTask(environment_id=env.id, conversation_id=key, request_key=request_key, config=config,
                     encrypted_prompt=seal(prompt, settings), encrypted_events=seal([], settings))
    db.add(task)
    db.flush()
    append_event(db, task, settings, 'queued', text='任务已创建，等待编程执行器。')
    db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                     action='agent.task.created', target_id=task.id))
    db.commit()
    return task


def create_agent_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/agent', tags=['programming-workbench'])

    def enabled():
        if not get_settings().agent_tasks_enabled:
            raise HTTPException(503, 'agent_workbench_unavailable')

    @router.get('/preferences', dependencies=[Depends(enabled)])
    def get_preferences(conversation_id: str = Query(default='default', pattern=r'^[a-zA-Z0-9_-]{1,64}$'),
                        principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        return preference(db, env.id, conversation_id)

    @router.put('/preferences', dependencies=[Depends(enabled)])
    def set_preferences(body: PreferenceInput, principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        if env.status == 'suspended':
            raise HTTPException(403, 'agent_account_suspended')
        if body.project_path:
            call_files(db, principal, get_settings(), 'GET', '/v1/directories', body.project_path)
        db.refresh(env, with_for_update=True)
        row = db.get(AgentPreference, (env.id, body.conversation_id))
        if body.revision != (row.revision if row else 0):
            raise HTTPException(409, 'agent_settings_changed')
        if row is None:
            row = AgentPreference(environment_id=env.id, conversation_id=body.conversation_id, revision=0)
            db.add(row)
        row.config = body.model_dump(exclude={'conversation_id', 'revision'})
        row.revision += 1
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                         action='agent.preferences.updated', target_id=env.id))
        db.commit()
        return preference(db, env.id, body.conversation_id)

    @router.get('/tasks', dependencies=[Depends(enabled)])
    def list_tasks(principal=Depends(principal_dependency), db=Depends(get_db)):
        env = own_environment(db, principal)
        rows = db.scalars(select(AgentTask).where(AgentTask.environment_id == env.id).order_by(AgentTask.created_at.desc()).limit(10))
        return {'tasks': [task_out(task, get_settings()) for task in rows]}

    def owned(db, principal, task_id):
        env = own_environment(db, principal)
        task = db.scalar(select(AgentTask).where(AgentTask.id == task_id, AgentTask.environment_id == env.id).with_for_update())
        if task is None:
            raise HTTPException(404, 'agent_task_not_found')
        return env, task

    @router.post('/tasks/{task_id}/decision', dependencies=[Depends(enabled)])
    def decide(task_id: str, body: DecisionInput, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, task = owned(db, principal, task_id)
        pending = task.pending or {}
        if env.status == 'suspended':
            raise HTTPException(403, 'agent_account_suspended')
        if task.status != 'awaiting_approval' or pending.get('id') != body.operation_id:
            raise HTTPException(409, 'agent_approval_expired')
        if pending.get('decision'):
            if pending['decision'] != body.decision:
                raise HTTPException(409, 'agent_approval_already_decided')
            return task_out(task, get_settings())
        if not pending.get('expires_at') or datetime.fromisoformat(pending['expires_at']) <= datetime.now(timezone.utc):
            raise HTTPException(409, 'agent_approval_expired')
        task.pending = {**pending, 'decision': body.decision}
        append_event(db, task, get_settings(), 'decision', operation_id=body.operation_id, decision=body.decision)
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                         action='agent.operation.' + body.decision, target_id=task.id + '/' + body.operation_id))
        db.commit()
        return task_out(task, get_settings())

    @router.post('/tasks/{task_id}/cancel', dependencies=[Depends(enabled)])
    def cancel(task_id: str, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, task = owned(db, principal, task_id)
        if task.status in ACTIVE and task.status != 'cancelling':
            task.status = 'cancelling'
            append_event(db, task, get_settings(), 'cancel', text='已请求停止任务，源文件保留。')
            db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                             action='agent.task.cancel', target_id=task.id))
        db.commit()
        return task_out(task, get_settings())

    return router
