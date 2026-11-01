import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from control_plane.database import get_db, make_engine
from control_plane.main import app
from control_plane.models import Base
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
        operation = next(op for op in client.get(path + "/operations", headers=auth).json() if op["resource_id"] == resource["id"])
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
