"""Quota admission and gateway behavior over an actual HTTP dependency."""
import json
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select

from control_plane.code_execution import reserve, usage
from control_plane.auth import Principal
from control_plane.models import CodeExecution, CapacityAccount, now
from control_plane.settings import get_settings
from test_api import harness
from test_capacity import capacity_harness

PREFIX = '/internal/v1/codeapi/'


@pytest.fixture
def code_harness(capacity_harness, monkeypatch):
    client, auth, env_id, observed, issuer = capacity_harness
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv('PLATFORM_CODE_EXECUTION_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_CODE_EXECUTION_PUBLIC_KEY', key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode())
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            status = 503 if body['code'] == 'ambiguous' else 400 if body['code'] == 'invalid' else 200
            self.send_response(status); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'stdout': '42', 'session_id': 'test-session'} if status == 200 else {'error': 'private-provider-diagnostic'}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv('PLATFORM_CODE_EXECUTION_URL', f'http://127.0.0.1:{server.server_port}/')
    get_settings.cache_clear()
    def signed(subject='alice', **extra):
        claims = dict(iss='librechat', aud='codeapi', sub='mongo-user', iat=int(time.time()), exp=int(time.time())+300,
                      platform_subject=subject, platform_issuer=issuer)
        return {'Authorization': 'Bearer ' + jwt.encode({**claims, **extra}, key, algorithm='EdDSA')}
    yield client, signed, env_id, calls, issuer
    server.shutdown(); server.server_close(); thread.join(); get_settings.cache_clear()


def test_limits_identity_idempotency_and_timeout(code_harness):
    client, signed, env_id, calls, issuer = code_harness
    assert client.post(PREFIX+'exec', json={'code':'1','lang':'py'}).status_code == 403
    assert client.post(PREFIX+'exec', headers=signed(platform_issuer='https://other'), json={'code':'1','lang':'py'}).status_code == 403
    assert client.post(PREFIX+'exec', headers=signed('victim'), json={'code':'1','lang':'py'}).status_code == 404
    assert client.post(PREFIX+'programmatic/exec', headers=signed(), json={}).status_code == 404
    header = {**signed(), 'Idempotency-Key': 'synthetic-execution-0001'}
    response = client.post(PREFIX+'exec', headers=header, json={'code':'1','lang':'py','timeout':900000})
    assert response.status_code == 200 and calls[0]['timeout'] == 10000
    assert client.post(PREFIX+'exec', headers=header, json={'code':'1','lang':'py'}).status_code == 409
    assert len(calls) == 1
    with client.session_factory.begin() as db:
        db.merge(CapacityAccount(environment_id=env_id, plan={'id':'existing','code_runs_monthly':1}))
    assert client.post(PREFIX+'exec', headers=signed(), json={'code':'1','lang':'py'}).status_code == 429
    with client.session_factory.begin() as db:
        account = db.get(CapacityAccount, env_id)
        account.plan = {'id':'basic', 'code_runs_monthly':2}
    assert client.post(PREFIX+'exec', headers=signed(), json={'code':'1','lang':'py'}).status_code == 200
    assert len(calls) == 2


def test_unclear_outcome_charged_no_retry_and_bounded_lease(code_harness):
    client, signed, env_id, calls, issuer = code_harness
    result = client.post(PREFIX+'exec', headers=signed(), json={'code':'ambiguous','lang':'py'})
    assert result.status_code == 503 and 'private-provider' not in result.text and len(calls) == 1
    assert client.post(PREFIX+'exec', headers=signed(), json={'code':'1','lang':'py'}).status_code == 429
    with client.session_factory.begin() as db:
        row = db.scalar(select(CodeExecution))
        assert row.state == 'unknown'
        row.lease_until = now()-timedelta(seconds=1)
    assert client.post(PREFIX+'exec', headers=signed(), json={'code':'1','lang':'py'}).status_code == 200
    with client.session_factory() as db:
        from control_plane.models import UserEnvironment
        state = usage(db, db.get(UserEnvironment, env_id))
        assert state['used_runs'] == 2


def test_definite_preexecution_rejection_not_charged_and_month_rollover(code_harness):
    client, signed, env_id, calls, issuer = code_harness
    assert client.post(PREFIX+'exec', headers=signed(), json={'code':'invalid','lang':'py'}).status_code == 400
    with client.session_factory.begin() as db:
        from control_plane.models import UserEnvironment
        assert usage(db, db.get(UserEnvironment, env_id))['used_runs'] == 0
        row = db.scalar(select(CodeExecution)); row.state = 'complete'; row.period = '2020-01'
        assert usage(db, db.get(UserEnvironment, env_id))['used_runs'] == 0


def test_upload_reservations_bounded_and_count_separate(code_harness):
    client, signed, env_id, calls, issuer = code_harness
    with client.session_factory.begin() as db:
        db.merge(CapacityAccount(environment_id=env_id, plan={'code_runs_monthly':1}))
    with client.session_factory() as db:
        row = reserve(db, Principal(issuer, 'alice'), 'synthetic-upload-0001', '0'*64, 'upload', 16*1024*1024)
        row.state = 'complete'; db.commit()
        with pytest.raises(Exception) as error:
            reserve(db, Principal(issuer, 'alice'), 'synthetic-upload-0002', '0'*64, 'upload', 1)
        assert error.value.status_code == 429
