"""Create-only workspace journal prototype; not connected to a provider.

Every external create is preceded by a committed in-flight state. A crash,
timeout, conflict, or invalid response leaves that state unchanged; later calls
must not retry or adopt a resource. There is no automatic recovery or deletion.
"""
from copy import deepcopy
from typing import Protocol

from sqlalchemy import select

from .models import UserEnvironment, WorkspaceProvisioningIntent, WorkspaceVolumeBinding
from .user_environments import identity_digest, plan_user_environment
from .workspace_binding import BindingMismatch, VolumeBinding, verify_volume_binding


class CreateProvider(Protocol):
    def create_namespace(self, manifest: dict) -> dict: ...
    def read_namespace(self, name: str) -> dict: ...
    def create_resource_quota(self, manifest: dict) -> dict: ...
    def read_resource_quota(self) -> dict: ...
    def create_network_policy(self, manifest: dict) -> dict: ...
    def read_network_policy(self) -> dict: ...
    def create_pvc(self, manifest: dict) -> dict: ...
    def inspect_claim_volume(self, expected_pvc_uid: str) -> tuple[dict, dict | None]: ...


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


def _quota_match(actual: dict, expected: dict, uid: str | None = None) -> str:
    found = _metadata_match(actual, expected, namespace_uid=uid)
    hard = actual.get('spec', {}).get('hard', {})
    if any(hard.get(k) != v for k, v in expected['spec']['hard'].items()):
        raise UnsafeProviderResponse('Quota does not match expected limits')
    return found


def _policy_match(actual: dict, expected: dict, uid: str | None = None) -> str:
    found = _metadata_match(actual, expected, namespace_uid=uid)
    spec = actual.get('spec', {})
    if (spec.get('podSelector') != {} or set(spec.get('policyTypes', [])) != {'Ingress', 'Egress'} or
            spec.get('ingress', []) or spec.get('egress', [])):
        raise UnsafeProviderResponse('Network policy is not default deny')
    return found


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
        if state in {'namespace_inflight', 'quota_inflight', 'policy_inflight', 'pvc_inflight', 'pvc_recorded'}:
            return state
        if env.status != 'requested':
            return 'environment_not_eligible'
        plan = plan_user_environment(env.issuer, env.subject, storage_gib=env.storage_gib)
        namespace = next(obj for obj in plan['objects'] if obj['kind'] == 'Namespace')
        pvc = next(obj for obj in plan['objects'] if obj['kind'] == 'PersistentVolumeClaim')
        quota = next(obj for obj in plan['objects'] if obj['kind'] == 'ResourceQuota')
        policy = next(obj for obj in plan['objects'] if obj['kind'] == 'NetworkPolicy')
        recorded_namespace_uid = intent.namespace_uid
        recorded_quota_uid = intent.quota_uid
        recorded_policy_uid = intent.policy_uid
        if state == 'requested':
            intent.state = 'namespace_inflight'
        elif state == 'namespace_recorded' and intent.namespace_uid:
            intent.state = 'quota_inflight'
        elif state == 'quota_recorded' and intent.namespace_uid and intent.quota_uid:
            intent.state = 'policy_inflight'
        elif state == 'policy_recorded' and intent.namespace_uid and intent.quota_uid and intent.policy_uid:
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
        if state == 'namespace_recorded':
            uid = _quota_match(provider.create_resource_quota(deepcopy(quota)), quota)
            next_state, field, expected_state = 'quota_recorded', 'quota_uid', 'quota_inflight'
        elif state == 'quota_recorded':
            _quota_match(provider.read_resource_quota(), quota, recorded_quota_uid)
            uid = _policy_match(provider.create_network_policy(deepcopy(policy)), policy)
            next_state, field, expected_state = 'policy_recorded', 'policy_uid', 'policy_inflight'
        else:
            _quota_match(provider.read_resource_quota(), quota, recorded_quota_uid)
            _policy_match(provider.read_network_policy(), policy, recorded_policy_uid)
            uid = _pvc_match(provider.create_pvc(deepcopy(pvc)), pvc)
            next_state, field, expected_state = 'pvc_recorded', 'pvc_uid', 'pvc_inflight'

    with session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.id == environment_id).with_for_update())
        intent = db.scalar(select(WorkspaceProvisioningIntent).where(WorkspaceProvisioningIntent.environment_id == environment_id).with_for_update())
        if env is None or intent is None or intent.state != expected_state:
            raise RuntimeError('Provisioning journal changed while create was in flight')
        setattr(intent, field, uid)
        intent.state = next_state
    return next_state


def observe_and_record_volume(session_factory, provider: CreateProvider, environment_id: str) -> str:
    """Record the observed bound PV for a trusted, previously journalled PVC.

    Returns ``pending`` or ``bound`` for storage only; it never marks the user
    environment ready. No provider mutations occur. The caller must recheck
    Kubernetes objects before using them because API reads are not atomic.
    """
    if getattr(provider, 'dry_run', False):
        raise ValueError('Dry-run providers cannot record volume bindings')
    with session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.id == environment_id).with_for_update())
        if env is None:
            raise ValueError('Unknown environment')
        intent = db.scalar(select(WorkspaceProvisioningIntent).where(WorkspaceProvisioningIntent.environment_id == environment_id).with_for_update())
        if intent is None or intent.state != 'pvc_recorded' or not all((intent.namespace_uid, intent.quota_uid, intent.policy_uid, intent.pvc_uid)):
            raise ValueError('Completed create journal is required')
        if env.status != 'requested':
            return 'environment_not_eligible'
        if env.identity_hash != identity_digest(env.issuer, env.subject):
            raise BindingMismatch('Environment identity record is inconsistent')
        snapshot = (env.issuer, env.subject, env.storage_gib, env.identity_hash, intent.state, intent.namespace_uid,
                    intent.quota_uid, intent.policy_uid, intent.pvc_uid)
    issuer, subject, storage_gib, _, _, ns_uid, quota_uid, policy_uid, pvc_uid = snapshot
    plan = plan_user_environment(issuer, subject, storage_gib=storage_gib)
    objects = {obj['kind']: obj for obj in plan['objects']}
    namespace = provider.read_namespace(plan['namespace'])
    _metadata_match(namespace, objects['Namespace'], namespace_uid=ns_uid)
    if namespace.get('status', {}).get('phase') != 'Active':
        return 'pending'
    _quota_match(provider.read_resource_quota(), objects['ResourceQuota'], quota_uid)
    _policy_match(provider.read_network_policy(), objects['NetworkPolicy'], policy_uid)
    pvc, pv = provider.inspect_claim_volume(pvc_uid)
    if _pvc_match(pvc, objects['PersistentVolumeClaim']) != pvc_uid:
        raise BindingMismatch('PVC UID changed')
    if pv is None:
        return 'pending'
    binding = VolumeBinding(namespace_name=plan['namespace'], namespace_uid=ns_uid, pvc_uid=pvc_uid,
                            pv_name=pv.get('metadata', {}).get('name'), pv_uid=pv.get('metadata', {}).get('uid'),
                            volume_handle=pv.get('spec', {}).get('csi', {}).get('volumeHandle'))
    if verify_volume_binding(issuer, subject, binding, namespace, pvc, pv) != 'ready':
        return 'pending'
    with session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.id == environment_id).with_for_update())
        intent = db.scalar(select(WorkspaceProvisioningIntent).where(WorkspaceProvisioningIntent.environment_id == environment_id).with_for_update())
        if env is None or intent is None or env.status != 'requested' or snapshot != (
                env.issuer, env.subject, env.storage_gib, env.identity_hash, intent.state, intent.namespace_uid,
                intent.quota_uid, intent.policy_uid, intent.pvc_uid):
            raise RuntimeError('Environment or provisioning journal changed during observation')
        existing = db.get(WorkspaceVolumeBinding, environment_id)
        if existing is not None:
            if any(getattr(existing, key) != value for key, value in vars(binding).items()):
                raise BindingMismatch('Recorded PV identity differs; refusing replacement')
        else:
            db.add(WorkspaceVolumeBinding(environment_id=environment_id, **vars(binding)))
    return 'bound'
