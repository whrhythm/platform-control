from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import select

from control_plane.models import ProjectDatabase, UserEnvironment, WorkspaceFileService, now
from control_plane.project_database_controller import reconcile_database, reconcile_databases
from control_plane.project_database_provider import DatabaseOwnershipError
from control_plane.project_databases import names
from control_plane.settings import get_settings
from control_plane.database_cli import child_environment
from control_plane.application_runtime import application_objects
from test_capacity import capacity_harness

PREFIX = '/internal/v1/workspace/databases'


@pytest.fixture
def databases(capacity_harness, monkeypatch, tmp_path):
    client, auth, *_ = capacity_harness
    monkeypatch.setenv('PLATFORM_PROJECT_DATABASES_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    ca = tmp_path / 'pg-ca.crt'
    ca.write_text('synthetic-public-ca')
    monkeypatch.setenv('PLATFORM_PROJECT_DATABASE_CA', str(ca))
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.project_databases.call_files', lambda *a, **k: httpx.Response(200, json={'entries': []}))
    with client.session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.subject == 'alice'))
        db.get(WorkspaceFileService, env.id).resources = {'RoleBinding': {'state': 'created'}}
    return client, auth


def request(client, auth, path='demo'):
    return client.post(PREFIX, headers=auth, json={'project_path': path})


def test_repeat_request_allowance_identity_and_delete_confirmation(databases):
    client, auth = databases
    first = request(client, auth)
    assert first.status_code == 202
    row = first.json()
    assert 'password' not in first.text and 'ccdb_' not in first.text
    assert request(client, auth).json()['id'] == row['id']
    assert request(client, auth, '../outside').status_code == 400
    assert request(client, auth, 'two').status_code == 202
    assert request(client, auth, 'three').status_code == 409
    assert client.post(PREFIX + '/' + row['id'] + '/retry', headers={**auth, 'X-CodeChat-Subject': 'bob'}).status_code == 404
    assert client.get(PREFIX + '/profile?path=demo', headers=auth).status_code == 404
    assert client.post(PREFIX + '/' + row['id'] + '/delete', headers=auth, json={'confirmation': 'wrong'}).status_code == 400
    assert client.post(PREFIX + '/' + row['id'] + '/delete', headers=auth, json={'confirmation': 'demo'}).status_code == 409


class Provider:
    def __init__(self):
        self.created, self.deleted = [], []
        self.failure = None

    def ensure(self, *args):
        self.created.append(args[:4])
        return {'database_oid': 11, 'owner_oid': 12, 'login_oid': 13}

    def size(self, *args):
        if self.failure:
            raise self.failure
        return 9 * 1024 ** 2

    def delete(self, *args):
        self.deleted.append(args)


class Cluster:
    def __init__(self):
        self.objects = {}

    def create(self, document):
        actual = deepcopy(document)
        actual['metadata']['uid'] = 'secret-uid'
        self.objects[actual['metadata']['name']] = actual
        return actual

    def get(self, path):
        return self.objects.get(path.split('/')[-1])

    def delete(self, path, uid):
        assert self.objects[path.split('/')[-1]]['metadata']['uid'] == uid
        del self.objects[path.split('/')[-1]]


def test_controller_delivery_metering_stale_and_explicit_destroy(databases):
    client, auth = databases
    identifier = request(client, auth).json()['id']
    provider, cluster, settings = Provider(), Cluster(), get_settings()
    for _ in range(3):
        reconcile_database(client.session_factory, identifier, cluster, settings, provider)
    assert len(provider.created) == 1 and len(cluster.objects) == 1
    listing = client.get(PREFIX, headers=auth).json()
    assert listing['databases'][0]['status'] == 'ready'
    assert listing['databases'][0]['used_bytes'] == 9 * 1024 ** 2
    account = client.get('/internal/v1/workspace/capacity', headers=auth).json()
    assert account['storage']['known_used_bytes'] == 12 + 9 * 1024 ** 2
    assert account['storage']['budget_bytes'] == (2 * 1024 + 256) * 1024 ** 2
    assert account['storage']['complete'] is False
    with client.session_factory.begin() as db:
        db.get(ProjectDatabase, identifier).observed_at = now() - timedelta(minutes=5)
    provider.failure = TimeoutError()
    reconcile_databases(client.session_factory, cluster, settings, provider)
    account = client.get('/internal/v1/workspace/capacity', headers=auth).json()
    assert account['storage']['known_used_bytes'] == 12
    assert account['storage']['components'][1]['status'] == 'stale'
    assert client.get(PREFIX, headers=auth).json()['databases'][0]['status'] == 'ready'
    result = client.post(PREFIX + '/' + identifier + '/delete', headers=auth, json={'confirmation': 'demo'})
    assert result.json()['status'] == 'deleting'
    for _ in range(2):
        reconcile_database(client.session_factory, identifier, cluster, settings, provider)
    assert client.get(PREFIX, headers=auth).json()['databases'][0]['status'] == 'deleted'
    with client.session_factory() as db:
        old_generation = db.get(ProjectDatabase, identifier).generation
    assert request(client, auth).json()['id'] == identifier
    with client.session_factory() as db:
        assert db.get(ProjectDatabase, identifier).generation != old_generation


def test_ambiguous_secret_write_never_adopted(databases):
    client, auth = databases
    identifier = request(client, auth).json()['id']
    with client.session_factory.begin() as db:
        row = db.get(ProjectDatabase, identifier)
        row.status, row.resources = 'provisioning', {'secret_state': 'inflight'}
    reconcile_databases(client.session_factory, Cluster(), get_settings(), Provider())
    row = client.get(PREFIX, headers=auth).json()['databases'][0]
    assert row['status'] == 'failed' and row['error_code'] == 'database_ownership_requires_review'
    assert client.post(PREFIX + '/' + identifier + '/retry', headers=auth).json()['status'] == 'failed'


def test_real_http_secret_delivery_and_uid_cleanup(databases):
    url = os.environ.get('CODECHAT_DELIVERY_SIM_URL')
    if not url:
        pytest.skip('Requires local Postman delivery simulation')
    from control_plane.application_controller import Cluster as HttpCluster
    client, auth = databases
    identifier = request(client, auth).json()['id']
    with httpx.Client(base_url=url.rstrip('/') + '/', timeout=5, trust_env=False) as http:
        http.post('__test__/seed', json={'objects': {}}).raise_for_status()
        cluster, provider = HttpCluster(http), Provider()
        for _ in range(3):
            reconcile_database(client.session_factory, identifier, cluster, get_settings(), provider)
        assert client.get(PREFIX, headers=auth).json()['databases'][0]['status'] == 'ready'
        snapshot = http.get('__test__/snapshot').json()
        assert len(snapshot['objects']) == 1 and snapshot['mutations'][0][0] == 'POST'
        client.post(PREFIX + '/' + identifier + '/delete', headers=auth, json={'confirmation': 'demo'})
        for _ in range(2):
            reconcile_database(client.session_factory, identifier, cluster, get_settings(), provider)
        assert http.get('__test__/snapshot').json()['objects'] == {}
        assert client.get(PREFIX, headers=auth).json()['databases'][0]['status'] == 'deleted'


def test_project_process_gets_database_but_not_platform_credentials(monkeypatch):
    monkeypatch.setenv('CODECHAT_MODEL_KEY', 'synthetic-platform-key')
    monkeypatch.setenv('AGENT_LLM_MODEL', 'internal-model')
    profile = {'environment': {'DATABASE_URL': 'postgresql://user:synthetic@postgres/db?sslmode=verify-full&sslrootcert=/old',
        'PGHOST': 'postgres', 'PGPORT': '5432', 'PGDATABASE': 'db', 'PGUSER': 'user', 'PGPASSWORD': 'synthetic',
        'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': '/old'}}
    child = child_environment(profile, '/temporary/ca.crt')
    assert child['PGSSLROOTCERT'] == '/temporary/ca.crt'
    assert 'sslrootcert=%2Ftemporary%2Fca.crt' in child['DATABASE_URL']
    assert 'CODECHAT_MODEL_KEY' not in child and 'AGENT_LLM_MODEL' not in child
    app = SimpleNamespace(id='a' * 36, image='registry/app@sha256:' + 'b' * 64,
                          revision='c' * 36, desired_state='running')
    objects = application_objects(app, SimpleNamespace(identity_hash='d' * 64), get_settings(), database_secret='database-test')
    container = objects[-1]['spec']['template']['spec']['containers'][0]
    assert all('valueFrom' in value and 'value' not in value for value in container['env'])
    assert container['volumeMounts'][0]['readOnly'] is True
    assert 'synthetic' not in str(objects)


def test_credential_profile_is_agent_only_and_not_cached(databases, monkeypatch):
    from control_plane.main import app
    from control_plane.programming import model_principal
    from control_plane.auth import Principal
    client, auth = databases
    identifier = request(client, auth).json()['id']
    with client.session_factory.begin() as db:
        row = db.get(ProjectDatabase, identifier)
        row.status = 'ready'
        env = db.get(UserEnvironment, row.environment_id)
        issuer = env.issuer
    monkeypatch.setitem(app.dependency_overrides, model_principal, lambda: Principal(issuer=issuer, subject='alice'))
    profile = client.get('/agent/v1/databases/profile?path=demo')
    assert profile.status_code == 200 and profile.headers['cache-control'] == 'no-store'
    assert profile.json()['environment']['PGSSLMODE'] == 'verify-full'
    assert profile.json()['ca'] == 'synthetic-public-ca'
    assert 'PGPASSWORD' not in client.get(PREFIX, headers=auth).text
