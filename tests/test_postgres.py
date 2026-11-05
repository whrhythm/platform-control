import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy import select, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from control_plane.database import get_db, make_engine
from control_plane.main import app
from control_plane.models import Base, Tenant, UserEnvironment, WorkspaceVolumeBinding, WorkspaceProvisioningIntent, CapacityAccount
from control_plane.workspace_provisioning import observe_and_record_volume, provision_one, request_intent
from control_plane.settings import get_settings
from control_plane.worker import plan_one
from test_resources import request, setup_tenant


@pytest.fixture
def postgres_harness(harness, monkeypatch):
    url = os.environ.get("PLATFORM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Run scripts/test_postgres.py for a disposable PostgreSQL")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost"} and parsed.database.startswith("platform_test_"), "Refusing a non-local/non-test database"
    monkeypatch.setenv("PLATFORM_DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    engine = make_engine(url)
    sessions = sessionmaker(engine, expire_on_commit=False)

    def test_db():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = test_db
    client, token = harness
    client.session_factory = sessions
    yield client, token
    # First provisioning now atomically persists trial rights. Remove only this
    # disposable fixture's snapshots before exercising guarded schema downgrade.
    with sessions.begin() as db:
        db.execute(delete(CapacityAccount))
    engine.dispose()
    command.downgrade(config, "base")
    get_settings.cache_clear()


def test_parallel_requests_never_exceed_tenant_capacity(postgres_harness):
    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)

    def create(index):
        return request(client, path, project, auth, {"kind": "workspace", "name": f"dev-{index}"}, f"parallel-request-{index}").status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(create, range(20)))
    assert statuses.count(201) == 8
    assert statuses.count(409) == 12
    reserved = client.get(path + "/quota", headers=auth).json()["reserved"]
    assert reserved["cpu_millicores"] == 4000
    assert reserved["memory_mib"] == 8192
    assert reserved["storage_gib"] == 40
    assert len(client.get(path + "/operations", headers=auth).json()) == 8


def test_parallel_code_execution_reservations_are_bounded(postgres_harness, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy import delete
    from control_plane.auth import Principal
    from control_plane.code_execution import reserve, usage
    from control_plane.models import CodeExecution, CapacityAccount, CapacityOrder, StorageMeasurement, WorkspaceFileService
    from test_capacity import prepare_capacity
    client, _ = postgres_harness
    client, _auth, env_id, _observed, issuer = prepare_capacity(client, monkeypatch)
    def submit(index):
        with client.session_factory() as db:
            try:
                reserve(db, Principal(issuer, 'alice'), f'parallel-code-{index:04d}', '0'*64)
                return 201
            except HTTPException as error:
                return error.status_code
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(16)))
        assert results.count(201) == 1 and results.count(429) == 15
        with client.session_factory() as db:
            assert usage(db, db.get(UserEnvironment, env_id))['used_runs'] == 1
    finally:
        with client.session_factory.begin() as db:
            for model in (CodeExecution, StorageMeasurement, CapacityOrder, CapacityAccount, WorkspaceFileService, WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id == env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))


def test_parallel_agent_submissions_create_one_task(postgres_harness, monkeypatch):
    import httpx
    from cryptography.fernet import Fernet
    from sqlalchemy import delete
    from control_plane.agent_tasks import submit_task
    from control_plane.auth import Principal
    from control_plane.models import AgentTask, AgentPreference
    client, _ = postgres_harness
    for name, value in {'WORKSPACE_FILES_ENABLED': 'true', 'AGENT_TASKS_ENABLED': 'true',
                        'LIBRECHAT_SERVICE_TOKEN': 'x' * 40,
                        'MODEL_CREDENTIAL_ENCRYPTION_KEY': Fernet.generate_key().decode()}.items():
        monkeypatch.setenv('PLATFORM_' + name, value)
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.agent_tasks.call_files', lambda *a, **kw: httpx.Response(200, json={'entries': []}))
    auth = {'X-CodeChat-Service-Token': 'x' * 40, 'X-CodeChat-Subject': 'agent-owner'}
    assert client.post('/internal/v1/workspace/ensure', headers=auth).status_code == 200
    assert client.put('/internal/v1/workspace/agent/preferences', headers=auth, json={
        'conversation_id': 'default', 'revision': 0, 'project_path': 'demo',
        'strategy': 'implement', 'approval': 'strict'}).status_code == 200
    def submit(_):
        with client.session_factory() as db:
            return submit_task(db, Principal(get_settings().oidc_issuer, 'agent-owner'), get_settings(),
                               'thread', 'synthetic instruction', 'synthetic-model').id
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(submit, range(16)))
        assert len(set(ids)) == 1
        with client.session_factory() as db:
            assert len(list(db.scalars(select(AgentTask)))) == 1
    finally:
        with client.session_factory.begin() as db:
            db.execute(delete(AgentTask))
            db.execute(delete(AgentPreference))
            identifiers = select(UserEnvironment.id).where(UserEnvironment.subject == 'agent-owner')
            db.execute(delete(CapacityAccount).where(CapacityAccount.environment_id.in_(identifiers)))
            db.execute(delete(WorkspaceProvisioningIntent).where(WorkspaceProvisioningIntent.environment_id.in_(identifiers)))
            db.execute(delete(UserEnvironment).where(UserEnvironment.subject == 'agent-owner'))


def test_parallel_idempotency_creates_one_resource(postgres_harness):
    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)

    def create(_):
        result = request(client, path, project, auth, {"kind": "workspace", "name": "dev"}, "shared-request-key")
        assert result.status_code == 201
        return result.json()["id"]
    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(pool.map(create, range(20)))
    assert len(set(ids)) == 1
    assert client.get(path + "/quota", headers=auth).json()["reserved"]["cpu_millicores"] == 500


def test_worker_and_cancellation_cannot_restore_cancelled_resource(postgres_harness):
    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)
    for index in range(10):
        resource = request(client, path, project, auth, {"kind": "workspace", "name": "dev"}, f"cancel-race-{index}").json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(plan_one, client.session_factory)
            cancellation = pool.submit(client.delete, path + "/resources/" + resource["id"], headers=auth)
            worker.result()
            assert cancellation.result().status_code == 200
        response = client.get(path + "/operations", headers=auth)
        assert response.status_code == 200, response.text
        operation = next(op for op in response.json() if op["resource_id"] == resource["id"])
        assert operation["status"] == "cancelled"
        assert operation["plan"] is None
        assert client.get(path + "/quota", headers=auth).json()["reserved"]["storage_gib"] == 0


def test_parallel_first_requests_create_one_environment(postgres_harness):
    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)
    url = f"{path}/projects/{project}/environment"

    def create(_):
        return client.post(url, headers=auth)

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(create, range(20)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    with client.session_factory() as db:
        accounts = list(db.scalars(select(CapacityAccount)))
        assert len(accounts) == 1 and accounts[0].plan['id'] == 'trial'


def test_volume_identifier_cannot_be_bound_to_two_users_concurrently(postgres_harness):
    client, _token = postgres_harness
    sessions = client.session_factory
    env_ids = []
    with sessions() as db:
        for user in ('alice-binding', 'bob-binding'):
            tenant = Tenant(name=user, owner_issuer='issuer', owner_subject=user, project_limit=1)
            db.add(tenant)
            db.flush()
            env = UserEnvironment(issuer='issuer', subject=user, identity_hash=user, storage_gib=2,
                                  first_tenant_id=tenant.id, first_project_id='project')
            db.add(env)
            db.flush()
            env_ids.append(env.id)
        db.commit()

    def insert(index):
        with sessions() as db:
            db.add(WorkspaceVolumeBinding(environment_id=env_ids[index], namespace_name=f'test-ns-{index}',
                                          namespace_uid='same-namespace-uid', pvc_uid=f'claim-{index}',
                                          pv_name=f'pv-{index}', pv_uid=f'pv-uid-{index}', volume_handle=f'volume-{index}'))
            try:
                db.commit()
                return 'created'
            except IntegrityError:
                db.rollback()
                return 'conflict'
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(insert, range(2))) == ['conflict', 'created']
    finally:
        with sessions() as db:
            for env_id in env_ids:
                binding = db.get(WorkspaceVolumeBinding, env_id)
                if binding is not None:
                    db.delete(binding)
            db.commit()


def test_parallel_provisioners_only_one_sends_create(postgres_harness):
    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)
    env_id = client.post(f'{path}/projects/{project}/environment', headers=auth).json()['id']
    sessions = client.session_factory
    request_intent(sessions, env_id)
    started, release = Event(), Event()

    class Provider:
        calls = 0

        def create_namespace(self, manifest):
            self.calls += 1
            started.set()
            assert release.wait(timeout=10)
            manifest['metadata']['uid'] = 'provider-created-ns'
            return manifest

    provider = Provider()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(provision_one, sessions, provider, env_id)
            try:
                assert started.wait(timeout=10)
                second = pool.submit(provision_one, sessions, provider, env_id)
                assert second.result(timeout=10) == 'namespace_inflight'
            finally:
                release.set()
            assert first.result(timeout=10) == 'namespace_recorded'
        assert provider.calls == 1
    finally:
        with sessions.begin() as db:
            intent = db.get(WorkspaceProvisioningIntent, env_id)
            if intent is not None:
                db.delete(intent)


def test_parallel_volume_observations_keep_one_binding(postgres_harness):
    from test_workspace_provisioning import FakeProvider

    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)
    env_id = client.post(f'{path}/projects/{project}/environment', headers=auth).json()['id']
    sessions = client.session_factory
    provider = FakeProvider()
    request_intent(sessions, env_id)
    for _ in range(4):
        provision_one(sessions, provider, env_id)
    provider.bind()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(lambda _: observe_and_record_volume(sessions, provider, env_id), range(2))) == ['bound', 'bound']
        with sessions() as db:
            binding = db.get(WorkspaceVolumeBinding, env_id)
            assert binding is not None and binding.volume_handle == 'volume-uid'
    finally:
        with sessions.begin() as db:
            binding = db.get(WorkspaceVolumeBinding, env_id)
            intent = db.get(WorkspaceProvisioningIntent, env_id)
            if binding is not None:
                db.delete(binding)
            if intent is not None:
                db.delete(intent)


def test_parallel_login_provisions_only_one_key(postgres_harness, monkeypatch):
    import httpx
    from cryptography.fernet import Fernet
    from control_plane import model_credentials
    from test_litellm import FakeGateway

    client, token = postgres_harness
    fake = FakeGateway()
    http_client = httpx.Client
    monkeypatch.setenv('PLATFORM_LITELLM_URL', 'http://litellm.test')
    monkeypatch.setenv('PLATFORM_LITELLM_MASTER_KEY', 'master')
    monkeypatch.setenv('PLATFORM_LITELLM_MODELS', 'model-a')
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('PLATFORM_LIBRECHAT_SERVICE_TOKEN', 's' * 40)
    get_settings.cache_clear()
    monkeypatch.setattr(model_credentials.httpx, 'Client', lambda **kw: http_client(transport=httpx.MockTransport(fake), **kw))
    headers = {'Authorization': 'Bearer ' + token('parallel-user'), 'X-CodeChat-Service-Token': 's' * 40}
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: client.post('/internal/v1/litellm/key', headers=headers), range(12)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()['key'] for response in responses}) == 1
    assert fake.calls.count(('POST', '/user/new')) == 1
    assert fake.calls.count(('POST', '/key/generate')) == 1
    with client.session_factory.begin() as db:
        environments = db.scalars(select(UserEnvironment).where(UserEnvironment.subject == 'parallel-user')).all()
        assert len(environments) == 1
        assert environments[0].first_project_id is None
        assert environments[0].status == 'requested'
        # This fixture uses a guarded, disposable local database only.
        db.delete(db.get(WorkspaceProvisioningIntent, environments[0].id))
        db.delete(db.get(CapacityAccount, environments[0].id))
        db.flush()
        db.delete(environments[0])


def test_parallel_capacity_orders_and_confirmations_are_idempotent(postgres_harness, monkeypatch):
    from sqlalchemy import delete, func
    from control_plane.models import CapacityAccount, CapacityOrder, StorageMeasurement, WorkspaceFileService
    from test_capacity import prepare_capacity, create_order, PREFIX
    client, _ = postgres_harness
    client, auth, env_id, _, _ = prepare_capacity(client, monkeypatch)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: create_order(client, auth), range(16)))
        assert all(response.status_code == 201 for response in responses)
        ids = {response.json()['id'] for response in responses}
        assert len(ids) == 1
        path = PREFIX + '/orders/' + ids.pop() + '/confirm-test'
        with ThreadPoolExecutor(max_workers=8) as pool:
            confirmations = list(pool.map(lambda _: client.post(path, headers=auth), range(16)))
        assert all(response.status_code == 200 and response.json()['attempt'] == 1 for response in confirmations)
        with client.session_factory() as db:
            assert db.scalar(select(func.count(CapacityOrder.id)).where(CapacityOrder.environment_id == env_id)) == 1
            assert db.get(UserEnvironment, env_id).storage_gib == 2
    finally:
        with client.session_factory.begin() as db:
            for model in (StorageMeasurement, CapacityOrder, CapacityAccount, WorkspaceFileService, WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id == env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))


def test_parallel_publications_cannot_overbook_compute(postgres_harness, monkeypatch):
    import httpx
    from sqlalchemy import delete
    from control_plane.models import CapacityAccount, UserApplication, StorageMeasurement, WorkspaceFileService
    from test_capacity import prepare_capacity, PREFIX
    client, _ = postgres_harness
    client, auth, env_id, _, _ = prepare_capacity(client, monkeypatch)
    monkeypatch.setenv('PLATFORM_PUBLISHER_ENABLED', 'true')
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.applications.call_files', lambda *a: httpx.Response(200,
        json={'entries': [{'name': 'Dockerfile', 'kind': 'file'}]}))
    client.get(PREFIX, headers=auth)
    with client.session_factory.begin() as db:
        account = db.get(CapacityAccount, env_id)
        account.plan = {**account.plan, 'application_limit': 20, 'applications_cpu_millicores': 500,
                        'applications_memory_mib': 512, 'build_concurrency': 4}
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda index: client.post('/internal/v1/workspace/apps',
                params={'path': f'project-{index}'}, headers=auth).status_code, range(16)))
        assert statuses.count(202) == 2 and statuses.count(409) == 14
        value = client.get(PREFIX, headers=auth).json()['quota_usage']
        assert value['application_cpu_millicores'] == 500
        assert value['application_memory_mib'] == 512
    finally:
        with client.session_factory.begin() as db:
            for model in (UserApplication, StorageMeasurement, CapacityAccount, WorkspaceFileService,
                          WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id == env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))


def test_projectless_migration_preserves_existing_workspace_binding(postgres_harness):
    client, _token = postgres_harness
    sessions = client.session_factory
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    with sessions.begin() as db:
        tenant = Tenant(name='migration', owner_issuer='issuer', owner_subject='migration', project_limit=1)
        db.add(tenant)
        db.flush()
        env = UserEnvironment(issuer='issuer', subject='migration', identity_hash='b' * 64,
                              storage_gib=2, first_tenant_id=tenant.id, first_project_id='existing-project')
        db.add(env)
        db.flush()
        env_id = env.id
        db.add(WorkspaceProvisioningIntent(environment_id=env_id, state='pvc_recorded'))
        db.add(WorkspaceVolumeBinding(environment_id=env_id, namespace_name='existing', namespace_uid='ns',
                                      pvc_uid='pvc', pv_name='pv', pv_uid='pv-uid', volume_handle='handle'))
    try:
        command.downgrade(config, '0007_workspace_isolation')
        command.upgrade(config, 'head')
        with sessions() as db:
            assert db.get(UserEnvironment, env_id).first_project_id == 'existing-project'
            assert db.get(WorkspaceProvisioningIntent, env_id).state == 'pvc_recorded'
            assert db.get(WorkspaceVolumeBinding, env_id).volume_handle == 'handle'
    finally:
        with sessions.begin() as db:
            for model in (WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                record = db.get(model, env_id)
                if record is not None:
                    db.delete(record)
