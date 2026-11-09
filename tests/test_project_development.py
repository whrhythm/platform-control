import asyncio
import json

import pytest
from sqlalchemy import select

from control_plane.agent_tasks import seal, submit_task
from control_plane.auth import Principal
from control_plane.development import prepare, relay
from control_plane.models import AgentTask, UserEnvironment, WorkspaceProject
from control_plane.settings import get_settings
from test_agent_workbench import workspace


@pytest.fixture
def project(workspace, monkeypatch):
    client, headers, base = workspace
    monkeypatch.setenv('PLATFORM_LITELLM_MODELS', 'model,coder')
    monkeypatch.setenv('PLATFORM_CAPABILITY_CHAT_MODEL', 'model')
    get_settings.cache_clear()
    with client.session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.subject == 'alice'))
        row = WorkspaceProject(environment_id=env.id, project_path='owned', name='Owned',
            encrypted_goal=seal('goal', get_settings()))
        db.add(row)
        db.flush()
        return client, headers, base, row.id, env.id


def test_project_settings_owner_scope_directory_model_and_snapshot(project):
    client, headers, base, identifier, env_id = project
    url = base + '/preferences'
    query = {'conversation_id': 'project-' + identifier}
    config = client.get(url, headers=headers, params=query).json()
    assert config['project_path'] == 'owned'
    assert client.get(url, headers={**headers, 'X-CodeChat-Subject': 'bob'}, params=query).status_code == 404
    assert client.put(url, headers=headers, json={**config, 'project_path': 'demo'}).status_code == 403
    assert client.put(url, headers=headers, json={**config, 'coding_model': 'unknown'}).status_code == 400
    result = client.put(url, headers=headers, json={**config, 'coding_model': 'coder'})
    assert result.status_code == 200, result.text
    assert client.put(url, headers=headers, json=config).status_code == 409
    with client.session_factory() as db:
        env = db.get(UserEnvironment, env_id)
        task = submit_task(db, Principal(env.issuer, env.subject), get_settings(), 'chat', 'write and test', None,
            project_id=identifier)
        original = dict(task.config)
        assert original['model'] == 'coder'
        assert submit_task(db, Principal(env.issuer, env.subject), get_settings(), 'chat', 'write and test', None,
            project_id=identifier).id == task.id
    assert client.put(url, headers=headers, json={**result.json(), 'strategy': 'plan'}).status_code == 200
    with client.session_factory() as db:
        assert db.get(AgentTask, task.id).config == original


class Upstream:
    def __init__(self, events):
        self.events = events

    async def aiter_lines(self):
        for event in self.events:
            yield 'data: ' + json.dumps({'choices': [{'index': 0, **event}]})
        yield 'data: [DONE]'


def run_stream(project, events):
    client, headers, base, identifier, env_id = project
    async def execute():
        with client.session_factory() as db:
            env = db.get(UserEnvironment, env_id)
            body = {'messages': [{'role': 'user', 'content': '修改项目并运行测试'}], 'stream': True}
            configured = prepare(db, env, identifier, body)
            assert configured['tools'][-1]['function']['name'] == 'develop_project'
            return ''.join([value async for value in relay(Upstream(events), db, env, get_settings(), identifier, 'chat', body)])
    return asyncio.run(execute())


def test_chat_does_not_start_runtime(project):
    text = run_stream(project, [{'delta': {'role': 'assistant', 'content': '你好'}}, {'delta': {}, 'finish_reason': 'stop'}])
    assert json.loads(text.splitlines()[0][6:])['choices'][0]['delta']['content'] == '你好'
    with project[0].session_factory() as db:
        assert not list(db.scalars(select(AgentTask)))


def test_real_tool_request_creates_owner_task_and_durable_reference(project):
    events = [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'develop_project', 'arguments': '{"instruction":'}}]}},
              {'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '"write and test"}'}}]}},
              {'delta': {}, 'finish_reason': 'tool_calls'}]
    first = run_stream(project, events)
    assert 'starbridge-task-' in first and '"tool_calls"' not in first
    assert run_stream(project, events) == first
    with project[0].session_factory() as db:
        tasks = list(db.scalars(select(AgentTask)))
        assert len(tasks) == 1
        assert tasks[0].environment_id == project[4]
        assert tasks[0].config['project_path'] == 'owned'


@pytest.mark.parametrize('arguments', ['{', 'null', '[]', '"not an object"'])
def test_malformed_call_cannot_start_runtime(project, arguments):
    text = run_stream(project, [{'delta': {'tool_calls': [{'function': {'name': 'develop_project', 'arguments': arguments}}]}},
                               {'delta': {}, 'finish_reason': 'tool_calls'}])
    assert '未启动' in text
    with project[0].session_factory() as db:
        assert not list(db.scalars(select(AgentTask)))


def test_other_runtime_tools_still_stream(project):
    text = run_stream(project, [{'delta': {'tool_calls': [{'function': {'name': 'calculator', 'arguments': '{}'}}]}},
                               {'delta': {}, 'finish_reason': 'tool_calls'}])
    assert 'calculator' in text
    with project[0].session_factory() as db:
        assert not list(db.scalars(select(AgentTask)))
