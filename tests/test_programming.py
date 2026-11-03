import hashlib
import asyncio
import os

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from control_plane.database import make_engine
from control_plane.models import Base, Tenant, ModelCredential, UserEnvironment, WorkspaceFileService, WorkspaceVolumeBinding
from control_plane.programming import agent_prompt, resolve_space, resolve_capability
from control_plane.litellm import LiteLLMGateway
from control_plane.settings import Settings
from control_plane.user_environments import identity_digest
from control_plane.workspace_agent_service import create_app, delivery_reply
from control_plane.workspace_runtime import file_service_objects


def test_capability_routes_preserve_legacy_and_reject_unconfigured_or_ambiguous_routes():
    settings = Settings(litellm_models='model', capabilities_enabled=True, capability_chat_model='model')
    assert resolve_capability('codechat-chat', settings) == ('chat', 'model')
    assert resolve_capability('codechat-coding', settings) == ('coding', None)
    assert resolve_capability('OpenHands', settings) == ('coding', None)
    assert resolve_capability('model', settings) == ('chat', 'model')
    for invalid in ('writing', 'music', 'http://other-service', {}, None):
        with pytest.raises(HTTPException) as exc:
            resolve_capability(invalid, settings)
        assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        resolve_capability('codechat-chat', Settings(litellm_models='model'))
    assert exc.value.status_code == 400
    for changes in ({'capability_chat_model': 'not-allowed'}, {'capability_coding_route': 'codechat-chat'}, {'capability_coding_route': 'model'}):
        with pytest.raises(HTTPException) as exc:
            resolve_capability('codechat-chat', settings.model_copy(update=changes))
        assert exc.value.status_code == 503


@pytest.mark.parametrize('route,coding', [('codechat-chat', False), ('codechat-coding', True), ('OpenHands', True)])
def test_http_capability_switch_forwards_same_history_and_issued_key(monkeypatch, route, coding):
    import json
    import certifi
    from fastapi import FastAPI
    from control_plane import programming
    captured = {}
    key = 'sk-' + 'k' * 48
    history = [{'role': 'user', 'content': 'Project alpha must print apricot'},
               {'role': 'assistant', 'content': 'Use alpha/main.py'},
               {'role': 'user', 'content': 'Implement the requirements above'}]
    async def space(db, settings, supplied, client, *, require_runtime):
        assert supplied == key
        captured['runtime'] = require_runtime
        return 'codechat-user-test', 'synthetic-space-token'
    def provider(request):
        captured['url'] = str(request.url)
        captured['body'] = json.loads(request.content)
        captured['authorization'] = request.headers['Authorization']
        return httpx.Response(200, content=b'data: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(programming.httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(provider)))
    monkeypatch.setattr(programming, 'resolve_space', space)
    settings = Settings(litellm_models='model', capabilities_enabled=True, capability_chat_model='model',
                        workspace_agent_image='agent', litellm_url='http://models', litellm_master_key='master',
                        workspace_service_ca=certifi.where())
    app = FastAPI()
    app.include_router(programming.router)
    class Database:
        def rollback(self):
            captured['released_transaction'] = True
    app.dependency_overrides[programming.get_db] = lambda: Database()
    app.dependency_overrides[programming.get_settings] = lambda: settings
    response = TestClient(app).post('/agent/v1/chat/completions', json={'model': route, 'stream': True, 'messages': history},
                                    headers={'Authorization': 'Bearer ' + key, 'X-CodeChat-Conversation': 'same-conversation'})
    assert response.status_code == 200 and '[DONE]' in response.text
    assert captured['runtime'] is coding and captured['released_transaction']
    if coding:
        assert captured['url'] == 'https://workspace-files.codechat-user-test.svc:8091/v1/run'
        assert captured['authorization'] == 'Bearer synthetic-space-token'
        assert captured['body']['api_key'] == key
        prompt = captured['body']['prompt']
        assert prompt.index('Project alpha must print apricot') < prompt.index('Use alpha/main.py') < prompt.index('Implement the requirements above')
    else:
        assert captured['url'] == 'http://models/v1/chat/completions'
        assert captured['authorization'] == 'Bearer ' + key
        assert captured['body']['model'] == 'model' and captured['body']['messages'] == history


def test_runner_writes_real_files_deduplicates_and_preserves_failed_tasks(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    state = tmp_path / 'state'
    calls = []
    def runner(prompt, key, workspace, url, model, emit, limit, cancel):
        calls.append(prompt)
        (workspace / 'hello.py').write_text('print("hello")\n')
        emit('正在写入文件\n')
        return '已创建 hello.py'
    client = TestClient(create_app(root, state, 's' * 48, 'http://models/v1', 'model', runner=runner))
    payload = {'task_id': 'a' * 64, 'prompt': 'create hello', 'api_key': 'sk-' + 'k' * 48}
    assert client.post('/v1/run', json=payload).status_code == 401
    response = client.post('/v1/run', json=payload, headers={'Authorization': 'Bearer ' + 's' * 48})
    assert response.status_code == 200 and '[DONE]' in response.text
    assert (root / 'hello.py').read_text() == 'print("hello")\n'
    assert 'hello.py' in response.text and payload['api_key'] not in response.text
    assert client.post('/v1/run', json=payload, headers={'Authorization': 'Bearer ' + 's' * 48}).status_code == 200
    assert len(calls) == 1
    (state / ('b' * 64 + '.json')).write_text('{"status":"running"}')
    assert client.post('/v1/run', json={**payload, 'task_id': 'b' * 64},
                       headers={'Authorization': 'Bearer ' + 's' * 48}).status_code == 409
    assert payload['api_key'] not in ''.join(p.read_text() for p in state.iterdir())


def test_runner_failure_is_visible_and_never_retried(tmp_path):
    def fail(*args):
        raise RuntimeError('provider echoed secret')
    client = TestClient(create_app(tmp_path, tmp_path / 'state', 's' * 48, 'http://models/v1', 'model', runner=fail))
    payload = {'task_id': 'c' * 64, 'prompt': 'create hello', 'api_key': 'sk-' + 'k' * 48}
    headers = {'Authorization': 'Bearer ' + 's' * 48}
    response = client.post('/v1/run', json=payload, headers=headers)
    assert '本轮编程未完成' in response.text and 'provider echoed' not in response.text
    assert client.post('/v1/run', json=payload, headers=headers).status_code == 409


def test_runner_timeout_finishes_stream_and_requests_cancellation(tmp_path):
    import threading
    stopped = threading.Event()
    def slow(prompt, key, workspace, url, model, emit, limit, cancel):
        assert cancel.wait(3)
        stopped.set()
        return 'cancelled'
    client = TestClient(create_app(tmp_path, tmp_path / 'state', 's' * 48, 'http://models/v1', 'model',
                                   runner=slow, max_seconds=0.05))
    response = client.post('/v1/run', json={'task_id': 'd' * 64, 'prompt': 'work', 'api_key': 'sk-' + 'k' * 48},
                           headers={'Authorization': 'Bearer ' + 's' * 48})
    assert '超过时间限制' in response.text and '[DONE]' in response.text
    assert stopped.wait(2)


def test_restarted_runner_marks_only_unfinished_task_as_interrupted(tmp_path):
    import json
    state = tmp_path / 'state'
    state.mkdir()
    (state / ('a' * 64 + '.json')).write_text('{"status":"running"}')
    (state / ('b' * 64 + '.json')).write_text('{"status":"completed","text":"kept"}')
    create_app(tmp_path, state, 's' * 48, 'http://models/v1', 'model')
    assert json.loads((state / ('a' * 64 + '.json')).read_text())['status'] == 'interrupted'
    assert json.loads((state / ('b' * 64 + '.json')).read_text())['text'] == 'kept'


def test_publication_receipt_finishes_turn_without_claiming_deployment_success():
    import json
    value = {'id': '0dc75d4e-20cb-4a6a-ae7f-b17fc0b1041c', 'status': 'building'}
    reply = delivery_reply('tool output\nCODECHAT_DELIVERY_RESULT ' + json.dumps(value))
    assert '后台继续执行' in reply and '不代表发布成功' in reply
    assert '未自动重复发布' in delivery_reply('CODECHAT_DELIVERY_RESULT ' + json.dumps({**value, 'status': 'failed'}))
    assert delivery_reply('CODECHAT_DELIVERY_RESULT invalid') is None
    assert delivery_reply('CODECHAT_DELIVERY_RESULT ' + json.dumps({**value, 'id': 'not-an-id'})) is None


def test_programming_manifest_mounts_same_disk_and_upgrade_changes_revision():
    digest = 'a' * 64
    image = 'harbor.k3s.local/internal/platform@sha256:' + 'b' * 64
    old = file_service_objects('codechat-user-' + digest[:40], digest, image, 's' * 48)
    new = file_service_objects('codechat-user-' + digest[:40], digest, image, 's' * 48,
        agent_image='harbor.k3s.local/internal/agent@sha256:' + 'c' * 64, llm_url='http://models:4000', llm_model='model',
        pip_index_url='https://packages.test/simple')
    pod = new[-1]['spec']['template']['spec']
    assert [c['name'] for c in pod['containers']] == ['files', 'agent']
    values = {value['name']: value['value'] for value in pod['containers'][1]['env']}
    assert values['PIP_INDEX_URL'] == values['UV_INDEX_URL'] == 'https://packages.test/simple'
    assert values['PIP_CACHE_DIR'].startswith('/workspace/')
    assert pod['volumes'][0]['persistentVolumeClaim']['claimName'] == 'workspace-data'
    assert old[-1]['spec']['template']['metadata'] != new[-1]['spec']['template']['metadata']
    assert not any(obj['kind'] == 'PersistentVolumeClaim' for obj in new)


def test_prompt_preserves_context_and_rejects_image_payloads():
    prompt = agent_prompt([{'role': 'assistant', 'content': 'created demo'}, {'role': 'user', 'content': 'edit demo'}])
    assert prompt.index('created demo') < prompt.index('edit demo')
    assert '10001:10001' in prompt and '0.0.0.0:8080' in prompt and 'COPY' in prompt
    with pytest.raises(HTTPException):
        agent_prompt([{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {}}]}])


@pytest.mark.parametrize('provider_mode', ['local', 'postman'])
def test_gateway_resolves_only_issued_key_and_recorded_space(tmp_path, provider_mode):
    simulation = os.environ.get('CODECHAT_LITELLM_SIM_URL') if provider_mode == 'postman' else None
    if provider_mode == 'postman' and not simulation:
        pytest.skip('requires local Postman simulation')
    encryption_key = Fernet.generate_key()
    cipher = Fernet(encryption_key)
    key = 'sk-' + 'k' * 48
    digest = identity_digest('issuer', 'subject')
    namespace = 'codechat-user-' + digest[:40]
    engine = make_engine('sqlite:///' + str(tmp_path / 'identity.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        tenant = Tenant(name='personal', owner_issuer='issuer', owner_subject='subject', project_limit=3)
        db.add(tenant)
        db.flush()
        env = UserEnvironment(issuer='issuer', subject='subject', identity_hash=digest, storage_gib=2, first_tenant_id=tenant.id)
        db.add(env)
        db.flush()
        db.add(ModelCredential(issuer='issuer', subject='subject', status='ready', encrypted_key=cipher.encrypt(key.encode()).decode()))
        db.add(WorkspaceFileService(environment_id=env.id, status='ready', encrypted_token=cipher.encrypt(b's' * 48).decode()))
        db.add(WorkspaceVolumeBinding(environment_id=env.id, namespace_name=namespace, namespace_uid='ns', pvc_uid='pvc',
            pv_name='pv', pv_uid='pvuid', volume_handle='disk'))
    def provider(request):
        assert request.url.params['key'] == hashlib.sha256(key.encode()).hexdigest()
        assert request.headers['Authorization'] == 'Bearer master'
        return httpx.Response(200, json={'info': {'user_id': 'codechat-' + digest}})
    settings = Settings(litellm_url=simulation or 'http://models', litellm_master_key='master', model_credential_encryption_key=encryption_key.decode())
    if simulation:
        with httpx.Client(base_url=simulation, timeout=5, trust_env=False) as client:
            gateway = LiteLLMGateway(client, 'master', ('model',))
            user_id = gateway.ensure_user('issuer', 'subject')
            gateway.ensure_key(user_id, key)
    async def verify():
        options = {'trust_env': False} if simulation else {'transport': httpx.MockTransport(provider)}
        async with httpx.AsyncClient(**options) as client:
            with sessions() as db:
                assert await resolve_space(db, settings, key, client) == (namespace, 's' * 48)
    asyncio.run(verify())
    engine.dispose()
