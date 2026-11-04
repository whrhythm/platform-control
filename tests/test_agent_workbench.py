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
