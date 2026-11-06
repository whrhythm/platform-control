"""Owner-authored acceptance, separate from agent execution and release readiness."""
from typing import Literal
import hashlib

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import AuditEvent, now
from .workspace_gateway import call_files


class Criterion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    text: str = Field(min_length=1, max_length=400)
    result: Literal['pending', 'passed', 'changes_requested'] = 'pending'

    @field_validator('text')
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError('empty criterion')
        return value.strip()


class AcceptanceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    goal: str = Field(max_length=4000)
    criteria: list[Criterion] = Field(max_length=12)
    note: str = Field(max_length=1000)

    @model_validator(mode='after')
    def distinct(self):
        if len({item.id for item in self.criteria}) != len(self.criteria):
            raise ValueError('duplicate criterion')
        return self


def original_request(messages):
    """Called only after the chat input has passed agent_prompt validation."""
    content = messages[-1]['content']
    text = '\n'.join(part['text'] for part in content) if isinstance(content, list) else content
    return {'text': text[:4000], 'truncated': len(text) > 4000}


def acceptance_out(task, settings):
    from .agent_tasks import unseal
    resources = task.resources or {}
    request = unseal(resources['user_request'], settings) if resources.get('user_request') else None
    record = resources.get('acceptance') or {}
    content = unseal(record['encrypted'], settings) if record else {
        'goal': request['text'] if request else '', 'criteria': [], 'note': ''}
    return {'task_id': task.id, 'project_path': task.config['project_path'], 'task_status': task.status,
            'request': request, 'revision': record.get('revision', 0),
            'source_sha256': record.get('source_sha256'),
            'updated_at': record.get('updated_at'), **content}


def acceptance_summary(task):
    record = (task.resources or {}).get('acceptance') or {}
    return {'revision': record.get('revision', 0), **record.get('counts', {
        'total': 0, 'passed': 0, 'pending': 0, 'changes_requested': 0})}


def save_acceptance(db, env, task, body, settings, principal):
    """Caller holds the task row lock; existing workers preserve unknown resource keys."""
    from .agent_tasks import ACTIVE, seal
    if env.status in ('suspended', 'deleted'):
        raise HTTPException(403, 'agent_account_suspended')
    current = acceptance_out(task, settings)
    content = body.model_dump(exclude={'revision'})
    previous = {key: current[key] for key in ('goal', 'criteria', 'note')}
    if body.revision != current['revision']:
        if body.revision == current['revision'] - 1 and content == previous:
            return current
        raise HTTPException(409, 'acceptance_changed')
    if task.status in ACTIVE and any(item.result != 'pending' for item in body.criteria):
        raise HTTPException(409, 'acceptance_task_active')
    old = {item['id']: item for item in current['criteria']}
    for item in body.criteria:
        changed = (body.goal != current['goal'] or item.id not in old or old[item.id]['text'] != item.text)
        if changed and item.result != 'pending':
            raise HTTPException(409, 'acceptance_reset_required')
    revision = current['revision'] + 1
    source = (task.resources or {}).get('acceptance', {}).get('source_sha256')
    checked = any(item.result != 'pending' for item in body.criteria)
    changed_results = any(item.result != 'pending' and (
        item.id not in old or old[item.id]['result'] != item.result) for item in body.criteria)
    if not checked:
        source = None
    elif changed_results:
        try:
            context = call_files(db, principal, settings, 'GET', '/v1/context', task.config['project_path']).content
        except HTTPException:
            raise HTTPException(503, 'acceptance_source_unavailable') from None
        digest = hashlib.sha256(context).hexdigest()
        if any(item['result'] != 'pending' for item in current['criteria']) and source != digest:
            raise HTTPException(409, 'acceptance_source_changed')
        source = digest
    counts = {'total': len(body.criteria), **{status: sum(item.result == status for item in body.criteria)
              for status in ('pending', 'passed', 'changes_requested')}}
    task.resources = {**(task.resources or {}), 'acceptance': {
        'version': 1, 'revision': revision, 'counts': counts, 'updated_at': now().isoformat(), 'source_sha256': source,
        'encrypted': seal(content, settings)}}
    task.updated_at = now()
    db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                     action='agent.acceptance.saved', target_id=f'{task.id}/{revision}'))
    db.commit()
    return acceptance_out(task, settings)
