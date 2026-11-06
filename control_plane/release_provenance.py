"""Immutable per-attempt evidence; never infer a release from mutable workspace files."""
from copy import deepcopy
import json
import re

from sqlalchemy import select

from .models import AgentTask, now


def freeze(db, environment_id, path):
    task = db.scalar(select(AgentTask).where(AgentTask.environment_id == environment_id,
        AgentTask.config['project_path'].as_string() == path,
        AgentTask.status.in_(('completed', 'failed', 'cancelled')),
        AgentTask.resources['acceptance']['revision'].as_integer() > 0
    ).order_by(AgentTask.updated_at.desc(), AgentTask.id).limit(1))
    record = (task.resources or {}).get('acceptance') if task else None
    return {'version': 1, 'requested_at': now().isoformat(), 'candidate': {
        'task_id': task.id, 'conversation_id': task.conversation_id, **deepcopy(record)
    } if record else None}


def build_source(cluster, app, job):
    pods = cluster.get('api/v1/namespaces/codechat-builds/pods?labelSelector=job-name%3D' + job['name'])
    found = []
    for pod in (pods or {}).get('items', []):
        meta = pod.get('metadata', {})
        if (meta.get('labels', {}).get('codechat.io/app-id') != app.id or not any(
            owner.get('kind') == 'Job' and owner.get('uid') == job['uid']
            for owner in meta.get('ownerReferences', []))):
            continue
        for container in pod.get('status', {}).get('initContainerStatuses', []):
            result = container.get('state', {}).get('terminated', {})
            if container.get('name') != 'source' or result.get('exitCode') != 0:
                continue
            try:
                digest = json.loads(result.get('message', '{}')).get('source_sha256', '')
            except (ValueError, AttributeError):
                continue
            if isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest):
                found.append(digest)
    return found[0] if len(found) == 1 else None


def public(record, settings):
    if not record:
        return None
    from .agent_tasks import unseal
    candidate = record.get('candidate')
    source = record.get('source_sha256')
    match = 'pending' if not source else 'none' if not candidate else (
        'unbound' if not candidate.get('source_sha256') else
        'matched' if candidate['source_sha256'] == source else 'changed')
    result = {key: record.get(key) for key in ('revision', 'source_sha256', 'image_digest', 'requested_at', 'confirmed_at')}
    result['acceptance_state'] = match
    result['acceptance'] = None
    if candidate:
        result['acceptance'] = {key: candidate.get(key) for key in ('task_id', 'conversation_id', 'revision', 'updated_at', 'counts')}
        if match == 'matched':
            content = unseal(candidate['encrypted'], settings)
            result['acceptance'].update(goal=content['goal'], criteria=content['criteria'])
    return result


def application_provenance(app, settings):
    return {'attempt': public(app.resources.get('provenance'), settings),
            'release': public(app.resources.get('last_good', {}).get('provenance'), settings)}
