from uuid import uuid4

from sqlalchemy import select

from control_plane.models import AIProjectBinding, AuditEvent, WorkspaceProject
from control_plane.settings import get_settings
from test_project_versions import versions

PREFIX = '/internal/v1/workspace/ai-projects'
OWNER = 'a' * 24


def setup(versions, monkeypatch):
    client, auth, env, root, forward = versions
    monkeypatch.setenv('PLATFORM_AI_PROJECTS_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_LITELLM_MODELS', 'test-model')
    monkeypatch.setenv('PLATFORM_CAPABILITY_CHAT_MODEL', 'test-model')
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.ai_projects.call_files', forward)
    return client, auth, root


def test_creation_recovery_idempotence_owner_and_preservation(versions, monkeypatch):
    client, auth, root = setup(versions, monkeypatch)
    body = {'request_id': str(uuid4()), 'name': '知识助手', 'purpose': '回答售后问题', 'native_owner_id': OWNER}
    first = client.post(PREFIX, headers=auth, json=body)
    assert first.status_code == 201, first.text
    record = first.json()
    project = record['project']['id']
    assert record['state'] == 'preparing' and 'initial_agent' not in record
    assert client.post(PREFIX, headers=auth, json=body).json() == record
    assert client.post(PREFIX, headers=auth, json={**body, 'purpose': 'changed'}).status_code == 409
    prepare = PREFIX + '/' + project + '/prepare'
    prepared = client.post(prepare, headers=auth, json={'native_owner_id': OWNER})
    assert prepared.status_code == 200, prepared.text
    initial = prepared.json()['initial_agent']
    assert initial['model'] == 'test-model' and not initial['tools']
    directory = root / record['project']['path']
    (directory / 'user.txt').write_text('keep this')
    assert client.post(prepare, headers=auth, json={'native_owner_id': OWNER}).status_code == 200
    assert (directory / 'user.txt').read_text() == 'keep this'
    assert client.post(prepare, headers=auth, json={'native_owner_id': 'b'*24}).status_code == 409
    assert client.post(prepare, headers={**auth, 'X-CodeChat-Subject': 'foreign'}, json={'native_owner_id': OWNER}).status_code == 404
    confirm = PREFIX + '/' + project + '/confirm'
    receipt = {'native_owner_id': OWNER, 'agent_id': record['agent_id']}
    assert client.post(confirm, headers=auth, json={**receipt, 'agent_id': 'agent_'+'0'*32}).status_code == 409
    assert client.post(confirm, headers=auth, json=receipt).json()['state'] == 'ready'
    assert client.post(confirm, headers=auth, json=receipt).json()['state'] == 'ready'
    assert client.get(PREFIX, headers=auth).json()['projects'][0]['state'] == 'ready'
    with client.session_factory() as db:
        assert len(list(db.scalars(select(AIProjectBinding)))) == 1
        assert len(list(db.scalars(select(WorkspaceProject)))) == 1
        assert list(db.scalars(select(AuditEvent.action).where(AuditEvent.action.like('ai_project.%')))) == [
            'ai_project.requested', 'ai_project.bound']


def test_disabled_feature_and_forged_client_identity(versions, monkeypatch):
    client, auth, _ = setup(versions, monkeypatch)
    body = {'request_id': str(uuid4()), 'name': 'test', 'purpose': 'test', 'native_owner_id': OWNER}
    assert client.post(PREFIX, headers=auth, json={**body, 'owner': 'foreign'}).status_code == 422
    assert client.post(PREFIX, json=body).status_code == 403
    monkeypatch.setenv('PLATFORM_AI_PROJECTS_ENABLED', 'false')
    get_settings.cache_clear()
    assert client.get(PREFIX, headers=auth).json() == {'enabled': False, 'projects': []}
    assert client.post(PREFIX, headers=auth, json=body).status_code == 404


def test_selected_root_preserves_existing_files_and_is_exclusive(versions, monkeypatch):
    client, auth, root = setup(versions, monkeypatch)
    monkeypatch.setattr('control_plane.workspace_projects.call_files', versions[-1])
    body = {'request_id': str(uuid4()), 'name': '售后项目', 'purpose': '知识助手',
            'native_owner_id': OWNER, 'path': 'project'}
    original = (root / 'project' / 'main.py').read_bytes()
    assert client.post(PREFIX, headers=auth, json={**body, 'path': '不存在'}).status_code == 422
    assert client.post(PREFIX, headers=auth, json={**body, 'path': 'missing'}).status_code == 404
    created = client.post(PREFIX, headers=auth, json=body)
    assert created.status_code == 201, created.text
    project = created.json()['project']
    assert project['path'] == 'project'
    assert client.post(PREFIX, headers=auth, json=body).json()['project']['id'] == project['id']
    assert client.post(PREFIX, headers=auth, json={**body, 'request_id': str(uuid4())}).status_code == 409
    assert client.put('/internal/v1/workspace/projects', headers=auth, json={
        'request_id': str(uuid4()), 'path': 'project', 'name': 'Another project', 'goal': '',
    }).json()['detail'] == 'projects_root_in_use'
    assert (root / 'project' / 'main.py').read_bytes() == original
