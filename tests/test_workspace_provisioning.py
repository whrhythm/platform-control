from copy import deepcopy

import pytest
from sqlalchemy.orm import sessionmaker

from control_plane.database import make_engine
from control_plane.models import Base, Tenant, UserEnvironment, WorkspaceProvisioningIntent, WorkspaceVolumeBinding
from control_plane.workspace_binding import BindingMismatch
from control_plane.user_environments import identity_digest
from control_plane.workspace_provisioning import UnsafeProviderResponse, observe_and_record_volume, provision_one, request_intent


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'intent.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        tenant = Tenant(name='alice', owner_issuer='issuer', owner_subject='alice', project_limit=1)
        db.add(tenant)
        db.flush()
        env = UserEnvironment(issuer='issuer', subject='alice', identity_hash=identity_digest('issuer', 'alice'), storage_gib=2,
                              first_tenant_id=tenant.id, first_project_id='project')
        db.add(env)
        db.flush()
        env_id = env.id
    yield sessions, env_id
    engine.dispose()


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.namespace = None
        self.quota = None
        self.policy = None
        self.pvc = None
        self.pv = None

    def create_namespace(self, manifest):
        self.calls.append('create_namespace')
        manifest['metadata']['uid'] = 'namespace-uid'
        manifest['status'] = {'phase': 'Active'}
        self.namespace = deepcopy(manifest)
        return manifest

    def read_namespace(self, name):
        self.calls.append('read_namespace')
        return deepcopy(self.namespace)

    def create_pvc(self, manifest):
        self.calls.append('create_pvc')
        manifest['metadata']['uid'] = 'pvc-uid'
        manifest['status'] = {'phase': 'Pending'}
        self.pvc = deepcopy(manifest)
        return manifest

    def inspect_claim_volume(self, expected_pvc_uid):
        self.calls.append('inspect_claim_volume')
        assert self.pvc['metadata']['uid'] == expected_pvc_uid
        return deepcopy(self.pvc), deepcopy(self.pv)

    def bind(self):
        self.pvc['status']['phase'] = 'Bound'
        self.pvc['spec']['volumeName'] = 'pv-qa'
        self.pv = {'metadata': {'name': 'pv-qa', 'uid': 'pv-uid'}, 'status': {'phase': 'Bound'},
                   'spec': {'storageClassName': 'codechat-workspace-retain', 'persistentVolumeReclaimPolicy': 'Retain',
                            'claimRef': {'name': 'workspace-data', 'namespace': self.pvc['metadata']['namespace'], 'uid': 'pvc-uid'},
                            'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'volume-uid'}}}

    def create_resource_quota(self, manifest):
        self.calls.append('create_quota')
        manifest['metadata']['uid'] = 'quota-uid'
        self.quota = deepcopy(manifest)
        return manifest

    def read_resource_quota(self):
        self.calls.append('read_quota')
        return deepcopy(self.quota)

    def create_network_policy(self, manifest):
        self.calls.append('create_policy')
        manifest['metadata']['uid'] = 'policy-uid'
        self.policy = deepcopy(manifest)
        return manifest

    def read_network_policy(self):
        self.calls.append('read_policy')
        return deepcopy(self.policy)


def test_journal_then_create_once_and_keep_environment_plan_only(store):
    sessions, env_id = store
    provider = FakeProvider()
    assert request_intent(sessions, env_id) == 'requested'
    assert request_intent(sessions, env_id) == 'requested'
    assert provision_one(sessions, provider, env_id) == 'namespace_recorded'
    assert provision_one(sessions, provider, env_id) == 'quota_recorded'
    assert provision_one(sessions, provider, env_id) == 'policy_recorded'
    assert provision_one(sessions, provider, env_id) == 'pvc_recorded'
    assert provision_one(sessions, provider, env_id) == 'pvc_recorded'
    assert provider.calls == ['create_namespace', 'read_namespace', 'create_quota', 'read_namespace', 'read_quota', 'create_policy', 'read_namespace', 'read_quota', 'read_policy', 'create_pvc']
    with sessions() as db:
        intent = db.get(WorkspaceProvisioningIntent, env_id)
        assert (intent.namespace_uid, intent.quota_uid, intent.policy_uid, intent.pvc_uid) == ('namespace-uid', 'quota-uid', 'policy-uid', 'pvc-uid')
        assert db.get(UserEnvironment, env_id).status == 'requested'


def test_crash_after_create_does_not_repeat_or_adopt(store):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    original = provider.create_namespace

    def crash_after_success(manifest):
        original(manifest)
        raise SystemExit('simulated crash after provider committed')

    provider.create_namespace = crash_after_success
    with pytest.raises(SystemExit):
        provision_one(sessions, provider, env_id)
    assert provider.namespace is not None
    assert provision_one(sessions, provider, env_id) == 'namespace_inflight'
    assert provider.calls == ['create_namespace']


def test_replaced_namespace_stops_before_creating_pvc(store):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    provision_one(sessions, provider, env_id)
    provider.namespace['metadata']['uid'] = 'attacker-replacement'
    with pytest.raises(UnsafeProviderResponse):
        provision_one(sessions, provider, env_id)
    assert provider.calls == ['create_namespace', 'read_namespace']
    assert provision_one(sessions, provider, env_id) == 'quota_inflight'


def test_pvc_uncertain_outcome_never_retries(store):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    provision_one(sessions, provider, env_id)
    provision_one(sessions, provider, env_id)
    provision_one(sessions, provider, env_id)

    def timeout_after_write(manifest):
        provider.calls.append('create_pvc')
        raise TimeoutError('response lost')

    provider.create_pvc = timeout_after_write
    with pytest.raises(TimeoutError):
        provision_one(sessions, provider, env_id)
    assert provision_one(sessions, provider, env_id) == 'pvc_inflight'
    assert provider.calls.count('create_pvc') == 1


def test_weakened_policy_stops_before_pvc_creation(store):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    for _ in range(3):
        provision_one(sessions, provider, env_id)
    provider.policy['spec']['egress'] = [{}]
    with pytest.raises(UnsafeProviderResponse):
        provision_one(sessions, provider, env_id)
    assert 'create_pvc' not in provider.calls
    assert provision_one(sessions, provider, env_id) == 'pvc_inflight'


def test_ineligible_environment_cannot_start_next_step(store):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    provision_one(sessions, provider, env_id)
    with sessions.begin() as db:
        db.get(UserEnvironment, env_id).status = 'suspended'
    assert provision_one(sessions, provider, env_id) == 'environment_not_eligible'
    assert provider.calls == ['create_namespace']


def test_observation_records_only_a_verified_bound_volume_and_is_idempotent(store):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    for _ in range(4):
        provision_one(sessions, provider, env_id)
    assert observe_and_record_volume(sessions, provider, env_id) == 'pending'
    with sessions() as db:
        assert db.get(WorkspaceVolumeBinding, env_id) is None
    provider.bind()
    assert observe_and_record_volume(sessions, provider, env_id) == 'bound'
    assert observe_and_record_volume(sessions, provider, env_id) == 'bound'
    with sessions() as db:
        bound = db.get(WorkspaceVolumeBinding, env_id)
        assert (bound.namespace_uid, bound.pvc_uid, bound.pv_uid, bound.volume_handle) == ('namespace-uid', 'pvc-uid', 'pv-uid', 'volume-uid')
        assert db.get(UserEnvironment, env_id).status == 'requested'


@pytest.mark.parametrize('failure', ['wrong_claim', 'wrong_policy', 'replaced_namespace', 'wrong_reclaim', 'wrong_volume'])
def test_unsafe_observation_never_records_or_replaces_binding(store, failure):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    for _ in range(4):
        provision_one(sessions, provider, env_id)
    provider.bind()
    assert observe_and_record_volume(sessions, provider, env_id) == 'bound'
    if failure == 'wrong_claim':
        provider.pv['spec']['claimRef']['uid'] = 'another-pvc'
    elif failure == 'wrong_policy':
        provider.policy['spec']['ingress'] = [{}]
    elif failure == 'replaced_namespace':
        provider.namespace['metadata']['uid'] = 'replaced'
    elif failure == 'wrong_reclaim':
        provider.pv['spec']['persistentVolumeReclaimPolicy'] = 'Delete'
    else:
        provider.pv['spec']['csi']['volumeHandle'] = 'different-volume'
    with pytest.raises((BindingMismatch, UnsafeProviderResponse)):
        observe_and_record_volume(sessions, provider, env_id)
    with sessions() as db:
        assert db.get(WorkspaceVolumeBinding, env_id).volume_handle == 'volume-uid'


def test_observation_rejects_changed_database_state_before_commit(store):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    for _ in range(4):
        provision_one(sessions, provider, env_id)
    provider.bind()
    original = provider.inspect_claim_volume

    def inspect_then_suspend(uid):
        result = original(uid)
        with sessions.begin() as db:
            db.get(UserEnvironment, env_id).status = 'suspended'
        return result

    provider.inspect_claim_volume = inspect_then_suspend
    with pytest.raises(RuntimeError, match='changed during observation'):
        observe_and_record_volume(sessions, provider, env_id)
    with sessions() as db:
        assert db.get(WorkspaceVolumeBinding, env_id) is None


def test_inconsistent_environment_identity_is_never_bound(store):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    for _ in range(4):
        provision_one(sessions, provider, env_id)
    provider.bind()
    with sessions.begin() as db:
        db.get(UserEnvironment, env_id).identity_hash = 'b' * 64
    with pytest.raises(BindingMismatch, match='identity record'):
        observe_and_record_volume(sessions, provider, env_id)
    with sessions() as db:
        assert db.get(WorkspaceVolumeBinding, env_id) is None
