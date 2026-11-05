"""Exercise account mutations over real HTTP, including ambiguous remote writes."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select
from control_plane.models import AuditEvent
from control_plane.settings import get_settings
from test_api import harness, headers


@pytest.fixture
def identity(harness, monkeypatch):
    rows = {1: dict(pk=1, uid='alice', username='alice', name='Alice', email='a@example.test',
                    is_active=True, type='internal', groups_obj=[], is_superuser=False)}
    state = {'fail_after_write': False, 'calls': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def handle_request(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
            assert self.headers['Authorization'] == 'Bearer synthetic-directory-token'
            state['calls'].append((self.command, self.path))
            code = 200
            if self.path.startswith('/core/users/?'):
                result = {'results': list(rows.values()), 'pagination': {'next': 0}}
            elif self.path == '/core/users/':
                result = dict(body, pk=len(rows)+1, uid='new-user', groups_obj=[], is_superuser=False)
                rows[result['pk']] = result
                code = 201
            else:
                row = rows[int(self.path.split('/')[3])]
                if self.path.endswith('/set_password/'):
                    assert not row['is_active'] or row['pk'] == 1
                    result = {}; code = 204
                elif self.command == 'PATCH':
                    row.update(body)
                    result = row
                    if state['fail_after_write']: code = 503; result = {'error':'secret-provider-diagnostic'}
                else: result = row
            self.send_response(code); self.send_header('Content-Type', 'application/json'); self.end_headers()
            if code != 204: self.wfile.write(json.dumps(result).encode())
        do_GET = do_POST = do_PATCH = handle_request

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv('PLATFORM_IDENTITY_ADMIN_URL', f'http://127.0.0.1:{server.server_port}/')
    monkeypatch.setenv('PLATFORM_IDENTITY_ADMIN_TOKEN', 'synthetic-directory-token')
    monkeypatch.setenv('PLATFORM_WORKSPACE_SERVICE_CA', '')
    get_settings.cache_clear()
    yield (*harness, rows, state)
    server.shutdown(); server.server_close(); thread.join(); get_settings.cache_clear()


def admin(token): return headers(token('operator', groups=['platform-admins']))


def test_create_edit_disable_and_reenable_existing_session(identity):
    client, token, rows, state = identity
    auth = admin(token)
    body = dict(username='new', name='New', email='new@example.test', password='Synthetic-Password-27', is_active=True)
    result = client.post('/v1/admin/accounts', headers=auth, json=body)
    assert result.status_code == 200, result.text
    assert 'password' not in result.text
    row = client.get('/v1/admin/accounts', headers=auth).json()['items'][0]
    body = {k: row[k] for k in ('username','name','email','revision')}
    body.update(is_active=False)
    result = client.put('/v1/admin/accounts/1', headers=auth, json=body)
    assert result.status_code == 200, result.text
    assert client.get('/v1/me', headers=headers(token())).status_code == 403
    body.update(is_active=True, revision=result.json()['revision'], name='Renamed')
    result = client.put('/v1/admin/accounts/1', headers=auth, json=body)
    assert result.status_code == 200, result.text
    assert client.get('/v1/me', headers=headers(token())).status_code == 200
    with client.session_factory() as db:
        actions = [row.action for row in db.scalars(select(AuditEvent))]
    assert 'identity.created' in actions and 'identity.updated' in actions


def test_uncertain_write_fences_existing_token_without_retry(identity):
    client, token, rows, state = identity
    auth = admin(token)
    row = client.get('/v1/admin/accounts', headers=auth).json()['items'][0]
    body = {k: row[k] for k in ('username','name','email','revision')}
    state['fail_after_write'] = True
    result = client.put('/v1/admin/accounts/1', headers=auth, json=body)
    assert result.status_code == 503
    assert 'secret-provider' not in result.text
    assert client.get('/v1/me', headers=headers(token())).status_code == 403
    assert sum(method == 'PATCH' for method, _ in state['calls']) == 1


def test_protected_users_conflicts_authorization_and_password_redaction(identity):
    client, token, rows, state = identity
    auth = admin(token)
    body = dict(username='alice', name='Alice', email='a@example.test', revision='stale')
    assert client.put('/v1/admin/accounts/1', headers=auth, json=body).status_code == 409
    rows[1]['is_superuser'] = True
    assert client.put('/v1/admin/accounts/1', headers=auth, json=body).status_code == 403
    assert client.get('/v1/admin/accounts', headers=headers(token())).status_code == 403
    body['password'] = 'secret'
    response = client.post('/v1/admin/accounts', headers=auth, json=body)
    assert response.status_code == 422 and 'secret' not in response.text
    assert not any(method in ('POST','PATCH') for method, _ in state['calls'])
