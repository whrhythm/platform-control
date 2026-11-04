"""Review an uncertain application write using read-only Kubernetes evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from .application_runtime import WRITE_REVIEW_CODE, object_path
from .models import AuditEvent, UserApplication, UserEnvironment

WRITE_ANNOTATION = 'codechat.io/write-attempt'
REVIEW_REQUESTED = 'application.review.requested'
REVIEW_RESUMED = 'application.review.resumed'
REVIEW_BLOCKED = 'application.review.blocked'
REVIEW_ACTIONS = (REVIEW_REQUESTED, REVIEW_RESUMED, REVIEW_BLOCKED)


def audit_target(app_id, review_id):
    return f'{app_id}/{review_id}'


def now():
    return datetime.now(timezone.utc)


def create_intent(document):
    meta = document['metadata']
    nonce = str(uuid4())
    meta.setdefault('annotations', {})[WRITE_ANNOTATION] = nonce
    return {
        'method': 'POST', 'kind': document['kind'], 'namespace': meta['namespace'],
        'name': meta['name'], 'nonce': nonce, 'labels': meta.get('labels', {}),
        **({'spec': document['spec']} if 'spec' in document and document['kind'] != 'Secret' else {}),
    }


def existing_intent(method, path, uid, operations=()):
    parts = path.split('/')
    return {'method': method, 'namespace': parts[-3], 'name': parts[-1], 'uid': uid,
            'kind': {'deployments': 'Deployment', 'apisixroutes': 'ApisixRoute',
                     'services': 'Service', 'networkpolicies': 'NetworkPolicy',
                     'jobs': 'Job'}.get(parts[-2]), 'operations': list(operations)}


def _subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            _subset(left, right) for left, right in zip(expected, actual))
    return expected == actual


def _field(document, path):
    for key in path.strip('/').split('/'):
        if not isinstance(document, dict) or key not in document:
            return None
        document = document[key]
    return document


def _workload_summary(document):
    template = _field(document, '/spec/template') or {}
    containers = _field(template, '/spec/containers') or []
    result = {'uid': _field(document, '/metadata/uid'), 'replicas': _field(document, '/spec/replicas'),
              'revision': template.get('metadata', {}).get('annotations', {}).get('codechat.io/revision'),
              'image': containers[0].get('image') if containers else None,
              'cpu_limit': containers[0].get('resources', {}).get('limits', {}).get('cpu') if containers else None,
              'memory_limit': containers[0].get('resources', {}).get('limits', {}).get('memory') if containers else None}
    return {k: v for k, v in result.items() if v is not None}


def public_intent(intent):
    public = {key: intent[key] for key in ('method', 'kind', 'namespace', 'name')}
    desired = {'uid': intent.get('uid'), **_workload_summary({'spec': intent.get('spec', {})})}
    for step in intent.get('operations', ()):
        if step['path'] == '/spec/replicas':
            desired['replicas'] = step['value']
        elif step['path'] == '/spec/template':
            desired.update(_workload_summary({'spec': {'template': step['value']}}))
        elif step['path'] == '/spec':
            desired['hosts'] = [host for rule in step['value'].get('http', ())
                                for host in rule.get('match', {}).get('hosts', ())]
    public['expected'] = {k: v for k, v in desired.items() if v is not None}
    return public


def inspect_write(cluster, intent, app_id):
    method, kind = intent['method'], intent['kind']
    if not kind or kind == 'Secret':
        # Build credentials are intentionally create-only for the controller.
        return {'result': 'unsupported', 'actual': {}, 'differences': ['metadata_unavailable']}
    actual = cluster.get(object_path(intent['namespace'], kind, intent['name']))
    summary = {'exists': actual is not None}
    if actual is None:
        if method == 'DELETE':
            return {'result': 'confirmed', 'actual': summary, 'differences': []}
        return {'result': 'unconfirmed', 'actual': summary, 'differences': ['object_missing']}
    meta = actual['metadata']
    summary.update(_workload_summary(actual))
    if kind == 'ApisixRoute':
        summary['hosts'] = [host for rule in actual.get('spec', {}).get('http', ())
                            for host in rule.get('match', {}).get('hosts', ())]
    labels = meta.get('labels', {})
    differences = []
    if labels.get('codechat.io/app-id') != app_id or not meta.get('uid') or (
            intent.get('uid') and meta.get('uid') != intent['uid']):
        differences.append('ownership_mismatch')
    elif method == 'POST':
        if (meta.get('annotations', {}).get(WRITE_ANNOTATION) != intent['nonce'] or
                not _subset(intent['labels'], labels) or meta.get('deletionTimestamp')):
            differences.append('attempt_mismatch')
        if 'spec' in intent and not _subset(intent['spec'], actual.get('spec')):
            differences.append('/spec')
    elif method == 'DELETE':
        differences.append('object_still_exists')
    elif method == 'PATCH':
        if meta.get('deletionTimestamp'):
            differences.append('object_terminating')
        differences.extend(step['path'] for step in intent['operations']
                           if not _subset(step['value'], _field(actual, step['path'])))
    return {'result': 'unconfirmed' if differences else 'confirmed', 'actual': summary, 'differences': differences}


def _resume(app, review, observed):
    resources = deepcopy(app.resources)
    intent = review['intent']
    if intent['method'] == 'POST':
        uid = observed['actual']['uid']
        if intent['kind'] == 'Job':
            resources['job'] = {'state': 'created', 'uid': uid, 'name': intent['name']}
        else:
            resources['workloads'][intent['kind']] = {'state': 'created', 'uid': uid}
    elif intent['method'] == 'DELETE':
        resources['workloads'][intent['kind']] = {'state': 'deleted'}
    resources.pop('write_review', None)
    app.resources = resources
    app.status = 'preparing' if review['prior_status'] == 'queued' else review['prior_status']
    app.error_code = None


def reconcile_review(sessions, app, cluster):
    review = app.resources.get('write_review')
    if not review or review['revision'] != app.revision or review['desired_state'] != app.desired_state:
        return
    request = review.get('request')
    old = review.get('evidence')
    if not request and old and (now() - datetime.fromisoformat(old['checked_at'])).total_seconds() < 20:
        return
    # All evidence reads happen before taking a database lock.
    observed = inspect_write(cluster, review['intent'], app.id)
    stable = old and all(observed[k] == old[k] for k in observed)
    evidence = {**observed, 'id': old['id'] if stable else str(uuid4()), 'checked_at': now().isoformat()}
    with sessions.begin() as db:
        current = db.get(UserApplication, app.id, with_for_update=True)
        if (current.status != 'failed' or current.error_code != WRITE_REVIEW_CODE or
                current.revision != review['revision'] or current.desired_state != review['desired_state'] or
                current.resources.get('write_review') != review):
            return
        if request:
            env = db.get(UserEnvironment, current.environment_id)
            confirmed = (env.status not in ('deleted', 'suspended') and observed['result'] == 'confirmed' and stable)
            db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=request['issuer'], subject=request['subject'],
                              action=REVIEW_RESUMED if confirmed else REVIEW_BLOCKED,
                              target_id=audit_target(app.id, review['id'])))
            if confirmed:
                _resume(current, review, observed)
                return
        current.resources = {**current.resources, 'write_review': {**review, 'evidence': evidence, 'request': None}}
