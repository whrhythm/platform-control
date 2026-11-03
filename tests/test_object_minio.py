import io
import json
import os
import secrets
from types import SimpleNamespace
from uuid import uuid4

import pytest

from control_plane.object_storage_bootstrap import bootstrap
from control_plane.object_storage_provider import MinioObjectProvider, ObjectOwnershipError
from control_plane.settings import Settings


@pytest.fixture(scope='module')
def server():
    endpoint = os.environ.get('CODECHAT_MINIO_TEST_ENDPOINT')
    if not endpoint:
        pytest.skip('Requires scripts/test_minio.py disposable local server')
    assert endpoint.startswith('127.0.0.1:') and endpoint.rsplit(':', 1)[1].isdigit()
    from minio import Minio, MinioAdmin
    from minio.credentials import StaticProvider
    root, password = os.environ['CODECHAT_MINIO_TEST_ROOT'], os.environ['CODECHAT_MINIO_TEST_PASSWORD']
    admin = MinioAdmin(endpoint=endpoint, credentials=StaticProvider(root, password), secure=False)
    parent, secret = 'codechat-s3-provider', secrets.token_urlsafe(36)
    bootstrap(admin, parent, secret)
    bootstrap(admin, parent, secret)
    settings = Settings(object_storage_endpoint=endpoint, object_storage_access_key=parent,
                        object_storage_secret_key=secret, object_storage_budget_mib=1)
    return SimpleNamespace(provider=MinioObjectProvider(settings), settings=settings, admin=admin,
        root=Minio(endpoint, access_key=root, secret_key=password, secure=False))


def binding():
    generation, env = str(uuid4()), str(uuid4())
    return SimpleNamespace(environment_id=env, generation=generation, bucket_name='cc-test-' + uuid4().hex,
        access_key='cc' + secrets.token_hex(9), resources={'endpoint': os.environ['CODECHAT_MINIO_TEST_ENDPOINT'], 'budget_bytes': 1024 ** 2})


def test_real_private_bucket_service_key_quota_versions_and_retention(server):
    from minio import Minio
    from minio.error import S3Error
    from minio.versioningconfig import VersioningConfig
    from minio.commonconfig import ENABLED
    row, other, secret = binding(), binding(), secrets.token_urlsafe(30)
    provider = server.provider
    provider.ensure_bucket(row)
    provider.ensure_bucket(row)
    provider.ensure_key(row, secret)
    provider.ensure_key(row, secret)
    provider.ensure_quota(row)
    provider.ensure_bucket(other)
    assert server.root.get_bucket_lifecycle(row.bucket_name) is None  # No automatic data expiration.
    server.root.set_bucket_versioning(row.bucket_name, VersioningConfig(ENABLED))
    child = Minio(server.settings.object_storage_endpoint, access_key=row.access_key, secret_key=secret, secure=False)
    child.put_object(row.bucket_name, 'project/file.txt', io.BytesIO(b'first'), 5)
    child.put_object(row.bucket_name, 'project/file.txt', io.BytesIO(b'second'), 6)
    child.put_object(row.bucket_name, 'project/other.txt', io.BytesIO(b'abc'), 3)
    result = provider.sample(row, 100)
    assert result['used_bytes'] == 14 and result['budget_bytes'] == 1024 ** 2
    response = child.get_object(row.bucket_name, 'project/file.txt')
    try:
        assert response.read() == b'second'
    finally:
        response.close(); response.release_conn()
    with pytest.raises(S3Error):
        child.put_object(other.bucket_name, 'foreign.txt', io.BytesIO(b'x'), 1)
    with pytest.raises(S3Error):
        child.get_bucket_tags(row.bucket_name)
    child.remove_object(row.bucket_name, 'project/file.txt')  # A delete marker must not erase old version accounting.
    assert provider.sample(row, 100)['used_bytes'] == 14
    with pytest.raises(Exception):
        provider.sample(row, 1)
    provider.verify_key(row, secret)
    assert json.loads(server.admin.bucket_quota_get(row.bucket_name))['quota'] == 1024 ** 2


def test_unsealed_bucket_collision_is_not_adopted(server):
    row = binding()
    server.root.make_bucket(row.bucket_name)
    with pytest.raises(ObjectOwnershipError):
        server.provider.ensure_bucket(row)
    assert server.root.get_bucket_tags(row.bucket_name) is None
