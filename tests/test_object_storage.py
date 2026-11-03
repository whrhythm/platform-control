from copy import deepcopy
from types import SimpleNamespace

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import select

from control_plane.application_runtime import application_objects
from control_plane.auth import Principal
from control_plane.main import app
from control_plane.models import UserEnvironment, UserObjectStorage, WorkspaceFileService
from control_plane.object_storage_controller import reconcile_object, reconcile_objects
from control_plane.object_storage_provider import ObjectOwnershipError, missing_admin_resource
from control_plane.programming import model_principal
from control_plane.settings import get_settings
from test_capacity import capacity_harness


@pytest.fixture
def objects(capacity_harness, monkeypatch):
    client, auth, env_id, *_ = capacity_harness
    monkeypatch.setenv('PLATFORM_OBJECT_STORAGE_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    get_settings.cache_clear()
    with client.session_factory.begin() as db:
        db.get(WorkspaceFileService, env_id).resources = {'RoleBinding': {'state': 'created'}}
    monkeypatch.setattr('control_plane.object_storage.call_files', lambda *a, **k: httpx.Response(200, json={'entries': []}))
    return client, auth, env_id


class Provider:
    def __init__(self):
        self.buckets, self.keys, self.quotas, self.failure = [], [], [], None
    def ensure_bucket(self, row):
        self.buckets.append(row.bucket_name)
    def ensure_key(self, row, secret):
        if self.failure:
            raise self.failure
        assert len(secret) == 40
        self.keys.append(row.access_key)
    def ensure_quota(self, row):
        self.quotas.append(row.bucket_name)


class Cluster:
    def __init__(self):
        self.secrets = []
    def create(self, document):
        result = deepcopy(document)
        result['metadata']['uid'] = 'owned-secret-uid'
        self.secrets.append(result)
        return result


def test_automatic_single_bucket_secret_profile_and_application_delivery(objects, monkeypatch):
    client, auth, env_id = objects
    provider, cluster, settings = Provider(), Cluster(), get_settings()
    for _ in range(7):
        reconcile_object(client.session_factory, env_id, cluster, settings, provider)
    assert len(provider.buckets) == len(provider.keys) == len(provider.quotas) == len(cluster.secrets) == 1
    public = client.get('/internal/v1/workspace/object-storage', headers=auth)
    assert public.json()['status'] == 'ready'
    assert 'AWS_' not in public.text and 'access_key' not in public.text
    with client.session_factory() as db:
        row = db.get(UserObjectStorage, env_id)
        issuer = db.get(UserEnvironment, env_id).issuer
        assert row.resources['secret_uid'] == 'owned-secret-uid'
    assert client.get('/internal/v1/workspace/object-storage/profile?path=demo', headers=auth).status_code == 404
    monkeypatch.setitem(app.dependency_overrides, model_principal, lambda: Principal(issuer=issuer, subject='alice'))
    response = client.get('/agent/v1/object-storage/profile?path=demo')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    profile = response.json()['environment']
    assert profile['CODECHAT_OBJECT_PREFIX'] == 'demo/' and profile['AWS_ACCESS_KEY_ID'] == row.access_key
    application = SimpleNamespace(id='a' * 36, revision='b' * 36, project_path='demo', desired_state='running', image='registry/test')
    runtime = application_objects(application, SimpleNamespace(identity_hash='c' * 64), settings, object_secret=cluster.secrets[0]['metadata']['name'])
    container = runtime[-1]['spec']['template']['spec']['containers'][0]
    assert next(value for value in container['env'] if value['name'] == 'AWS_SECRET_ACCESS_KEY')['valueFrom']['secretKeyRef']['key'] == 'AWS_SECRET_ACCESS_KEY'
    assert profile['AWS_SECRET_ACCESS_KEY'] not in str(runtime)


def test_retry_reuses_owned_generation_and_secret(objects):
    client, auth, env_id = objects
    provider, cluster, settings = Provider(), Cluster(), get_settings()
    for _ in range(2):
        reconcile_object(client.session_factory, env_id, cluster, settings, provider)
    with client.session_factory() as db:
        before = db.get(UserObjectStorage, env_id)
        generation, encrypted = before.generation, before.encrypted_secret
    provider.failure = TimeoutError()
    reconcile_objects(client.session_factory, cluster, settings, provider)
    assert client.get('/internal/v1/workspace/object-storage', headers=auth).json()['status'] == 'failed'
    assert client.post('/internal/v1/workspace/object-storage/retry', headers={**auth, 'X-CodeChat-Subject': 'bob'}).status_code == 404
    client.post('/internal/v1/workspace/object-storage/retry', headers=auth)
    provider.failure = None
    for _ in range(4):
        reconcile_objects(client.session_factory, cluster, settings, provider)
    with client.session_factory() as db:
        after = db.get(UserObjectStorage, env_id)
        assert after.status == 'ready' and after.generation == generation and after.encrypted_secret == encrypted
    assert len(provider.buckets) == 1


def test_ambiguous_secret_is_not_adopted(objects):
    client, auth, env_id = objects
    provider, cluster, settings = Provider(), Cluster(), get_settings()
    reconcile_object(client.session_factory, env_id, cluster, settings, provider)
    with client.session_factory.begin() as db:
        row = db.get(UserObjectStorage, env_id)
        row.resources = {**row.resources, 'phase': 'secret', 'secret_state': 'inflight'}
    reconcile_objects(client.session_factory, cluster, settings, provider)
    assert client.get('/internal/v1/workspace/object-storage', headers=auth).json()['status'] == 'requires_review'
    assert client.post('/internal/v1/workspace/object-storage/retry', headers=auth).json()['status'] == 'requires_review'
    assert cluster.secrets == []


def test_admin_absence_requires_exact_404_not_authentication_failure():
    from minio.error import MinioAdminException
    assert missing_admin_resource(MinioAdminException('404', '{"Code":"Missing"}'), ('Missing',))
    assert not missing_admin_resource(MinioAdminException('403', '{"Code":"Missing"}'), ('Missing',))
    assert not missing_admin_resource(MinioAdminException('404', 'invalid JSON'), ('Missing',))
