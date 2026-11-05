from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select

from control_plane.agent_execution import TaskTools, TaskStopped
from control_plane.agent_runtime import command_job
from control_plane.agent_tasks import AgentConfig, submit_task, unseal, now
from control_plane.agent_worker import recover
from control_plane.auth import Principal
from control_plane.models import AgentTask, UserEnvironment
from control_plane.settings import get_settings, Settings


@pytest.fixture
def workspace(harness, monkeypatch):
    client, _ = harness
    for key, value in {'WORKSPACE_FILES_ENABLED': 'true', 'AGENT_TASKS_ENABLED': 'true',
                       'LIBRECHAT_SERVICE_TOKEN': 's' * 40,
                       'MODEL_CREDENTIAL_ENCRYPTION_KEY': Fernet.generate_key().decode()}.items():
        monkeypatch.setenv('PLATFORM_' + key, value)
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.agent_tasks.call_files', lambda *a, **kw: httpx.Response(200, json={'entries': []}))
    headers = {'X-CodeChat-Service-Token': 's' * 40, 'X-CodeChat-Subject': 'alice'}
    for subject in ('alice', 'bob'):
        assert client.post('/internal/v1/workspace/ensure', headers={**headers, 'X-CodeChat-Subject': subject}).status_code == 200
    base = '/internal/v1/workspace/agent'
    config = {'conversation_id': 'default', 'revision': 0, 'project_path': 'demo', 'strategy': 'implement', 'approval': 'strict'}
    assert client.put(base + '/preferences', headers=headers, json=config).status_code == 200
    return client, headers, base


def create(workspace, prompt='private prompt'):
    client, headers, base = workspace
    with client.session_factory() as db:
        return submit_task(db, Principal(get_settings().oidc_issuer, 'alice'), get_settings(), 'conversation', prompt, 'model-one')


def test_preferences_scope_cas_and_task_idempotency(workspace):
    client, headers, base = workspace
    alice = client.get(base + '/preferences', headers=headers).json()
    assert set(alice) == {'conversation_id', 'revision', 'project_path', 'strategy', 'approval', 'mcp_servers'}
    assert alice['revision'] == 1 and alice['project_path'] == 'demo'
    bob = client.get(base + '/preferences', headers={**headers, 'X-CodeChat-Subject': 'bob'}).json()
    assert bob['project_path'] == ''
    assert client.put(base + '/preferences', headers=headers, json={k: v for k, v in alice.items() if k not in ('runtime', 'mcp_connections')} | {'revision': 0}).status_code == 409
    task = create(workspace)
    assert create(workspace).id == task.id
    with pytest.raises(HTTPException) as error:
        create(workspace, 'another request')
    assert error.value.status_code == 409
    assert 'private prompt' not in task.encrypted_prompt
    assert unseal(task.encrypted_prompt, get_settings()) == 'private prompt'
    assert client.get(base + '/tasks', headers={**headers, 'X-CodeChat-Subject': 'bob'}).json() == {'tasks': []}
    assert client.post(base + '/tasks/' + task.id + '/cancel', headers={**headers, 'X-CodeChat-Subject': 'bob'}).status_code == 404
    assert client.post(base + '/tasks/' + task.id + '/cancel', headers=headers).json()['status'] == 'cancelling'


def test_approval_expiry_and_one_decision(workspace):
    client, headers, base = workspace
    task = create(workspace)
    operation = str(uuid4())
    def pending(deadline):
        with client.session_factory.begin() as db:
            item = db.get(AgentTask, task.id)
            item.status = 'awaiting_approval'
            item.pending = {'id': operation, 'expires_at': deadline.isoformat()}
    url = base + '/tasks/' + task.id + '/decision'
    body = {'operation_id': operation, 'decision': 'approve'}
    pending(now() - timedelta(seconds=1))
    assert client.post(url, headers=headers, json=body).status_code == 409
    pending(now() + timedelta(minutes=1))
    assert client.post(url, headers=headers, json=body).status_code == 200
    assert client.post(url, headers=headers, json=body).status_code == 200
    assert client.post(url, headers=headers, json={**body, 'decision': 'reject'}).status_code == 409


def test_restart_never_replays_and_readonly_tools_refuse_writes(workspace):
    client, headers, base = workspace
    task = create(workspace)
    with client.session_factory.begin() as db:
        item = db.get(AgentTask, task.id)
        item.status = 'running'
        item.config = {**item.config, 'strategy': 'review'}
    tools = TaskTools(client.session_factory, task.id, None, get_settings(), None)
    with pytest.raises(TaskStopped, match='read_only'):
        tools.authorize('command', {'command': 'touch unexpected'})
    recover(client.session_factory, get_settings())
    result = client.get(base + '/tasks', headers=headers).json()['tasks'][0]
    assert result['status'] == 'interrupted'
    assert result['error_code'] == 'agent_interrupted_verify_files'
    with pytest.raises(TaskStopped):
        tools.authorize('write', {'path': 'unexpected'})


@pytest.mark.parametrize('service', ['database', 'objects'])
def test_service_credentials_require_confirmation_even_in_project_auto(workspace, monkeypatch, service):
    client, _, _ = workspace
    task = create(workspace)
    with client.session_factory.begin() as db:
        item = db.get(AgentTask, task.id)
        item.status = 'running'
        item.config = {**item.config, 'approval': 'workspace_auto'}
    def observe_wait(_):
        with client.session_factory() as db:
            item = db.get(AgentTask, task.id)
            assert item.status == 'awaiting_approval'
            assert item.pending['decision'] is None
        raise TaskStopped('confirmation_observed')
    monkeypatch.setattr('control_plane.agent_execution.time.sleep', observe_wait)
    tools = TaskTools(client.session_factory, task.id, None, get_settings(), None)
    with pytest.raises(TaskStopped, match='confirmation_observed'):
        tools.authorize('command', {'command': 'python query.py', service: True})


def test_large_file_read_is_explicitly_rejected_not_silently_truncated(workspace, monkeypatch):
    import json
    client, _, _ = workspace
    task = create(workspace)
    with client.session_factory.begin() as db:
        db.get(AgentTask, task.id).status = 'running'
    monkeypatch.setattr('control_plane.agent_execution.call_files',
                        lambda *a, **kw: httpx.Response(200, content=b'x' * 32769))
    tools = TaskTools(client.session_factory, task.id, None, get_settings(), None)
    assert json.loads(tools.file('read', 'large.txt'))['error'] == 'file_too_large_for_agent_read'


def test_historical_task_read_is_owner_scoped_and_has_no_list_window(workspace):
    client, headers, base = workspace
    oldest = create(workspace)
    for index in range(12):
        with client.session_factory.begin() as db:
            for task in db.scalars(select(AgentTask)):
                task.status = 'completed'
        create(workspace, f'new instruction {index}')
    assert oldest.id not in [t['id'] for t in client.get(base + '/tasks', headers=headers).json()['tasks']]
    assert client.get(base + '/tasks/' + oldest.id, headers=headers).json()['id'] == oldest.id
    assert client.get(base + '/tasks/' + oldest.id, headers={**headers, 'X-CodeChat-Subject': 'bob'}).status_code == 404
    with client.session_factory() as db:
        assert len(list(db.scalars(select(AgentTask)))) == 13


def test_task_references_restore_real_context_without_cross_owner_disclosure(workspace):
    from control_plane.agent_tasks import append_event, restore_task_messages
    client, _, _ = workspace
    task = create(workspace)
    reference = f'[编程任务](#starbridge-task-{task.id})'
    messages = [{'role': 'assistant', 'content': reference}, {'role': 'user', 'content': reference}]
    with client.session_factory.begin() as db:
        item = db.get(AgentTask, task.id)
        item.status = 'completed'
        append_event(db, item, get_settings(), 'finished', text='Actual test output: apricot')
    with client.session_factory() as db:
        restored = restore_task_messages(messages, db, task.environment_id, get_settings())
        assert 'apricot' in restored[0]['content']
        assert restored[1] == messages[1]
        assert messages[0]['content'] == reference
        bob = db.scalar(select(UserEnvironment).where(UserEnvironment.subject == 'bob'))
        assert 'apricot' not in restore_task_messages(messages, db, bob.id, get_settings())[0]['content']
        first = submit_task(db, Principal(get_settings().oidc_issuer, 'alice'), get_settings(), 'next',
                            'resolved output A', 'model-one', request_prompt='stable original request')
        second = submit_task(db, Principal(get_settings().oidc_issuer, 'alice'), get_settings(), 'next',
                             'resolved output B', 'model-one', request_prompt='stable original request')
        assert first.id == second.id


def test_chat_stream_persists_one_task_reference_without_waiting_or_resubmitting():
    import asyncio
    import json
    from control_plane.programming import task_stream
    task_id = str(uuid4())
    async def collect():
        return [chunk async for chunk in task_stream(task_id, Settings())]
    chunks = asyncio.run(collect())
    content = ''.join(json.loads(chunk[6:])['choices'][0]['delta'].get('content', '')
                      for chunk in chunks if chunk.startswith('data: {'))
    assert content == f'[编程任务](#starbridge-task-{task_id})'
    assert chunks[-1] == 'data: [DONE]\n\n'


def test_completed_task_context_reaches_ordinary_chat_provider(workspace, monkeypatch):
    import certifi
    import json
    from control_plane import programming
    from control_plane.agent_tasks import append_event
    client, _, _ = workspace
    task = create(workspace)
    with client.session_factory.begin() as db:
        item = db.get(AgentTask, task.id)
        item.status = 'completed'
        append_event(db, item, get_settings(), 'finished', text='Observed output: apricot-739')
    for key, value in {'WORKSPACE_SERVICE_CA': certifi.where(), 'WORKSPACE_AGENT_IMAGE': 'synthetic',
                       'LITELLM_URL': 'http://model', 'LITELLM_MASTER_KEY': 'synthetic',
                       'LITELLM_MODELS': 'model-one'}.items():
        monkeypatch.setenv('PLATFORM_' + key, value)
    get_settings.cache_clear()
    async def identity(db, settings, key, provider):
        return db.get(UserEnvironment, task.environment_id)
    observed = {}
    def upstream(request):
        observed.update(json.loads(request.content))
        return httpx.Response(200, content=b'data: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(programming, 'resolve_identity', identity)
    monkeypatch.setattr(programming.httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(upstream)))
    response = client.post('/agent/v1/chat/completions', headers={'Authorization': 'Bearer synthetic', 'X-CodeChat-Capability': 'chat'},
        json={'model': 'model-one', 'stream': True, 'messages': [
            {'role': 'assistant', 'content': f'[编程任务](#starbridge-task-{task.id})'},
            {'role': 'user', 'content': 'What was the actual output?'}]})
    assert response.status_code == 200
    assert 'apricot-739' in observed['messages'][0]['content']
    assert observed['messages'][1]['content'] == 'What was the actual output?'


@pytest.mark.parametrize('terminal', ['cancelled', 'interrupted', 'completed'])
def test_late_runner_result_cannot_overwrite_confirmed_terminal_state(workspace, terminal):
    from control_plane.agent_worker import finish
    from control_plane.agent_tasks import append_event
    client, _, _ = workspace
    task = create(workspace)
    with client.session_factory.begin() as db:
        item = db.get(AgentTask, task.id)
        item.status = terminal
        append_event(db, item, get_settings(), 'finished', text='confirmed result', status=terminal)
    finish(client.session_factory, task.id, get_settings(), 'failed', 'late_result', 'wrong result')
    with client.session_factory() as db:
        item = db.get(AgentTask, task.id)
        assert item.status == terminal
        finals = [e for e in unseal(item.encrypted_events, get_settings()) if e['kind'] == 'finished']
        assert len(finals) == 1 and finals[0]['text'] == 'confirmed result'


@pytest.mark.parametrize('path', ['../other', '/root', 'x/../y', 'x//y', 'x\\y'])
def test_project_traversal_is_rejected(path):
    with pytest.raises(ValueError):
        AgentConfig(project_path=path)


def test_command_has_only_project_mount_and_no_credentials():
    settings = Settings(agent_command_image='harbor.k3s.local/internal/command@sha256:' + 'a' * 64)
    env = SimpleNamespace(identity_hash='b' * 64)
    binding = SimpleNamespace(namespace_name='codechat-user-' + 'b' * 40, pvc_uid='known-pvc')
    task = SimpleNamespace(id=str(uuid4()), config={'project_path': 'projects/demo'})
    job = command_job(task, env, binding, str(uuid4()), 'python -m pytest', settings)
    pod = job['spec']['template']['spec']
    assert all(len(value) <= 63 for value in job['metadata']['labels'].values())
    assert pod['runtimeClassName'] == 'kata-qemu-runtime-rs'
    assert pod['automountServiceAccountToken'] is False
    assert job['spec']['backoffLimit'] == 0
    assert len(pod['volumes']) == 1
    container = pod['containers'][0]
    assert container['volumeMounts'] == [{'name': 'project', 'mountPath': '/project', 'subPath': 'files/projects/demo'}]
    assert 'envFrom' not in container
    assert not any(word in str(container['env']).lower() for word in ('token', 'key', 'password'))
    assert container['securityContext']['allowPrivilegeEscalation'] is False
    assert container['securityContext']['readOnlyRootFilesystem'] is True


def test_committed_job_timeout_observes_without_replaying(workspace):
    import os
    from control_plane.application_controller import Cluster
    from control_plane.models import WorkspaceVolumeBinding
    url = os.environ.get('CODECHAT_DELIVERY_SIM_URL')
    if not url:
        pytest.skip('requires local Postman delivery simulation')
    client, _, _ = workspace
    task = create(workspace)
    settings = get_settings().model_copy(update={'agent_command_image': 'harbor.k3s.local/internal/command@sha256:' + 'a' * 64})
    with client.session_factory.begin() as db:
        item = db.get(AgentTask, task.id)
        item.status = 'running'
        item.config = {**item.config, 'approval': 'workspace_auto'}
        env = db.get(UserEnvironment, item.environment_id)
        namespace = 'codechat-user-' + env.identity_hash[:40]
        db.add(WorkspaceVolumeBinding(environment_id=env.id, namespace_name=namespace, namespace_uid='ns',
            pvc_uid='expected-pvc', pv_name='pv', pv_uid='pv-uid', volume_handle='volume'))
    collection = f'apis/batch/v1/namespaces/{namespace}/jobs'
    with httpx.Client(base_url=url, timeout=10, trust_env=False) as provider:
        provider.post('__test__/seed', json={'objects': {
            f'api/v1/namespaces/{namespace}/persistentvolumeclaims/workspace-data': {'metadata': {'uid': 'expected-pvc'}},
            f'api/v1/namespaces/{namespace}/pods': {'items': []}},
            'delay': {'path': collection, 'method': 'POST', 'ms': 800}}).raise_for_status()
        provider.timeout = httpx.Timeout(0.25)
        tools = TaskTools(client.session_factory, task.id, Cluster(provider), settings, None)
        tools.command('echo synthetic')
        provider.timeout = httpx.Timeout(10)
        state = provider.get('__test__/snapshot').json()
    assert [entry[0] for entry in state['mutations']] == ['POST', 'DELETE']
    with client.session_factory() as db:
        item = db.get(AgentTask, task.id)
        assert item.resources['cleanup']['status'] == 'complete'
        assert item.resources['job'] is None
        assert len([e for e in unseal(item.encrypted_events, settings) if e['kind'] == 'terminal']) == 1
