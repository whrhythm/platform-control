import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select

from control_plane import model_credentials
from control_plane.models import AuditEvent, ModelCredential
from control_plane.settings import get_settings
from test_litellm import FakeGateway


def test_login_provisions_isolated_keys_once_and_does_not_restore_revoked(harness, monkeypatch):
    client, token = harness
    fake = FakeGateway()
    http_client = httpx.Client
    monkeypatch.setenv('PLATFORM_LITELLM_URL', 'http://litellm.test')
    monkeypatch.setenv('PLATFORM_LITELLM_MASTER_KEY', 'master')
    monkeypatch.setenv('PLATFORM_LITELLM_MODELS', 'model-a')
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('PLATFORM_LIBRECHAT_SERVICE_TOKEN', 's' * 40)
    get_settings.cache_clear()
    monkeypatch.setattr(model_credentials.httpx, 'Client', lambda **kw: http_client(transport=httpx.MockTransport(fake), **kw))
    path = '/internal/v1/litellm/key'
    def hdr(subject):
        return {'Authorization': 'Bearer ' + token(subject), 'X-CodeChat-Service-Token': 's' * 40}
    assert client.post(path).status_code == 401
    assert client.post(path, headers={'Authorization': 'Bearer ' + token('alice')}).status_code == 403
    a = client.post(path, headers=hdr('alice'))
    assert a.status_code == 200
    assert a.headers['cache-control'] == 'no-store'
    assert client.post(path, headers=hdr('alice')).json() == a.json()
    b = client.post(path, headers=hdr('bob'))
    assert b.status_code == 200 and a.json()['key'] != b.json()['key']
    assert len(fake.users) == len(fake.keys) == 2
    with client.session_factory() as db:
        records = db.scalars(select(ModelCredential)).all()
        assert len(records) == 2 and all(record.status == 'ready' for record in records)
        assert all(a.json()['key'] not in record.encrypted_key for record in records)
        assert len(db.scalars(select(AuditEvent).where(AuditEvent.action == 'model_credential.created')).all()) == 2
    fake.keys.clear()  # Simulate an administrator's explicit key revocation.
    assert client.post(path, headers=hdr('alice')).status_code == 503
    assert not fake.keys
    fake.users.clear()  # Removing the user must not re-issue its initial budget.
    assert client.post(path, headers=hdr('alice')).status_code == 503
    assert not fake.users
