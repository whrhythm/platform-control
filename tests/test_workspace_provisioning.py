from copy import deepcopy

import pytest
from sqlalchemy.orm import sessionmaker

from control_plane.database import make_engine
from control_plane.models import Base, Tenant, UserEnvironment, WorkspaceProvisioningIntent
from control_plane.workspace_provisioning import UnsafeProviderResponse, provision_one, request_intent


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'intent.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        tenant = Tenant(name='alice', owner_issuer='issuer', owner_subject='alice', project_limit=1)
        db.add(tenant)
        db.flush()
        env = UserEnvironment(issuer='issuer', subject='alice', identity_hash='a' * 64, storage_gib=2,
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

    def create_namespace(self, manifest):
        self.calls.append('create_namespace')
        manifest['metadata']['uid'] = 'namespace-uid'
        self.namespace = deepcopy(manifest)
        return manifest

    def read_namespace(self, name):
        self.calls.append('read_namespace')
        return deepcopy(self.namespace)

    def create_pvc(self, manifest):
        self.calls.append('create_pvc')
        manifest['metadata']['uid'] = 'pvc-uid'
        return manifest


def test_journal_then_create_once_and_keep_environment_plan_only(store):
    sessions, env_id = store
    provider = FakeProvider()
    assert request_intent(sessions, env_id) == 'requested'
    assert request_intent(sessions, env_id) == 'requested'
    assert provision_one(sessions, provider, env_id) == 'namespace_recorded'
    assert provision_one(sessions, provider, env_id) == 'pvc_recorded'
    assert provision_one(sessions, provider, env_id) == 'pvc_recorded'
    assert provider.calls == ['create_namespace', 'read_namespace', 'create_pvc']
    with sessions() as db:
        intent = db.get(WorkspaceProvisioningIntent, env_id)
        assert (intent.namespace_uid, intent.pvc_uid) == ('namespace-uid', 'pvc-uid')
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
    assert provision_one(sessions, provider, env_id) == 'pvc_inflight'


def test_pvc_uncertain_outcome_never_retries(store):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    provision_one(sessions, provider, env_id)

    def timeout_after_write(manifest):
        provider.calls.append('create_pvc')
        raise TimeoutError('response lost')

    provider.create_pvc = timeout_after_write
    with pytest.raises(TimeoutError):
        provision_one(sessions, provider, env_id)
    assert provision_one(sessions, provider, env_id) == 'pvc_inflight'
    assert provider.calls.count('create_pvc') == 1


def test_ineligible_environment_cannot_start_next_step(store):
    sessions, env_id = store
    provider = FakeProvider()
    request_intent(sessions, env_id)
    provision_one(sessions, provider, env_id)
    with sessions.begin() as db:
        db.get(UserEnvironment, env_id).status = 'suspended'
    assert provision_one(sessions, provider, env_id) == 'environment_not_eligible'
    assert provider.calls == ['create_namespace']
