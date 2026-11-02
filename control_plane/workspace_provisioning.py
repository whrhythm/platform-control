"""Create-only workspace journal prototype; not connected to a provider.

Every external create is preceded by a committed in-flight state. A crash,
timeout, conflict, or invalid response leaves that state unchanged; later calls
must not retry or adopt a resource. There is no automatic recovery or deletion.
"""
from copy import deepcopy
from typing import Protocol

from sqlalchemy import select

from .models import UserEnvironment, WorkspaceProvisioningIntent
from .user_environments import plan_user_environment


class CreateProvider(Protocol):
    def create_namespace(self, manifest: dict) -> dict: ...
    def read_namespace(self, name: str) -> dict: ...
    def create_pvc(self, manifest: dict) -> dict: ...


class UnsafeProviderResponse(ValueError):
    pass


def request_intent(session_factory, environment_id: str) -> str:
    """Idempotently journal an existing environment; no provider call is made.

    Caller must authenticate and authorize beforehand; this internal function
    intentionally does not implement a user-facing API.
    """
    with session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.id == environment_id).with_for_update())
        if env is None:
            raise ValueError('Unknown environment')
        intent = db.get(WorkspaceProvisioningIntent, environment_id)
        if intent is None:
            if env.status != 'requested':
                raise ValueError('Environment is not eligible for provisioning')
            intent = WorkspaceProvisioningIntent(environment_id=environment_id, state='requested')
            db.add(intent)
        return intent.state


def _metadata_match(actual: dict, expected: dict, *, namespace_uid: str | None = None) -> str:
    meta = actual.get('metadata', {})
    exp = expected['metadata']
    uid = meta.get('uid')
    if (not isinstance(uid, str) or not uid.strip() or meta.get('name') != exp['name'] or
            meta.get('namespace') != exp.get('namespace') or meta.get('deletionTimestamp') or
            any(meta.get('labels', {}).get(k) != v for k, v in exp.get('labels', {}).items()) or
            any(meta.get('annotations', {}).get(k) != v for k, v in exp.get('annotations', {}).items()) or
            (namespace_uid is not None and uid != namespace_uid)):
        raise UnsafeProviderResponse('Provider object identity does not match expected resource')
    return uid


def _pvc_match(actual: dict, expected: dict) -> str:
    uid = _metadata_match(actual, expected)
    spec = actual.get('spec', {})
    planned = expected['spec']
    if (spec.get('storageClassName') != planned['storageClassName'] or
            spec.get('accessModes') != planned['accessModes'] or
            spec.get('resources', {}).get('requests', {}).get('storage') != planned['resources']['requests']['storage']):
        raise UnsafeProviderResponse('Provider PVC configuration does not match expected resource')
    return uid


def provision_one(session_factory, provider: CreateProvider, environment_id: str) -> str:
    """Perform at most one external create, or return an existing state.

    No resource status is marked ready. The provider must implement create-only
    operations (never apply/replace); any error is an uncertain outcome. Until
    a human-reviewed recovery mechanism exists, in-flight states stay blocked.
    """
    with session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.id == environment_id).with_for_update())
        if env is None:
            raise ValueError('Unknown environment')
        intent = db.scalar(select(WorkspaceProvisioningIntent).where(WorkspaceProvisioningIntent.environment_id == environment_id).with_for_update())
        if intent is None:
            raise ValueError('Provisioning intent is missing')
        state = intent.state
        if state in {'namespace_inflight', 'pvc_inflight', 'pvc_recorded'}:
            return state
        if env.status != 'requested':
            return 'environment_not_eligible'
        plan = plan_user_environment(env.issuer, env.subject, storage_gib=env.storage_gib)
        namespace = next(obj for obj in plan['objects'] if obj['kind'] == 'Namespace')
        pvc = next(obj for obj in plan['objects'] if obj['kind'] == 'PersistentVolumeClaim')
        if state == 'requested':
            intent.state = 'namespace_inflight'
            recorded_namespace_uid = None
        elif state == 'namespace_recorded' and intent.namespace_uid:
            recorded_namespace_uid = intent.namespace_uid
            intent.state = 'pvc_inflight'
        else:
            raise ValueError('Invalid provisioning state')

    # The transaction above is committed before the provider sees the request.
    if state == 'requested':
        actual = provider.create_namespace(deepcopy(namespace))
        uid = _metadata_match(actual, namespace)
        next_state, field, expected_state = 'namespace_recorded', 'namespace_uid', 'namespace_inflight'
    else:
        observed = provider.read_namespace(namespace['metadata']['name'])
        _metadata_match(observed, namespace, namespace_uid=recorded_namespace_uid)
        actual = provider.create_pvc(deepcopy(pvc))
        uid = _pvc_match(actual, pvc)
        next_state, field, expected_state = 'pvc_recorded', 'pvc_uid', 'pvc_inflight'

    with session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.id == environment_id).with_for_update())
        intent = db.scalar(select(WorkspaceProvisioningIntent).where(WorkspaceProvisioningIntent.environment_id == environment_id).with_for_update())
        if env is None or intent is None or intent.state != expected_state:
            raise RuntimeError('Provisioning journal changed while create was in flight')
        setattr(intent, field, uid)
        intent.state = next_state
    return next_state
