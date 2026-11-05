from types import SimpleNamespace
import httpx

from control_plane.agent_stream import AnswerStream
from control_plane.agent_execution import TaskTools
from control_plane.agent_tasks import unseal
from control_plane.models import AgentTask
from control_plane.settings import get_settings
from test_agent_workbench import workspace, create


def test_public_answer_streams_partial_finish_json_and_ignores_other_arguments():
    values = []
    stream = AnswerStream(SimpleNamespace(progress=lambda *args, **data: values.append(data)), clock=lambda: 1)
    def chunk(delta, finish=None):
        stream({'id': 'first', 'choices': [{'delta': delta, 'finish_reason': finish}]})
    chunk({'reasoning_content': 'private chain', 'tool_calls': [{'index': 0, 'function': {'name': 'ProjectTool', 'arguments': '{"content":"private file"}'}}]})
    assert values == []
    chunk({'tool_calls': [{'index': 1, 'function': {'name': 'finish', 'arguments': '{"message":"你好'}}]})
    assert values == [{'text': '你好'}]
    chunk({'tool_calls': [{'index': 1, 'function': {'arguments': '\\n世界"}'}}]}, 'tool_calls')
    assert values[-1] == {'text': '你好\n世界'}
    assert 'private' not in str(values)


def test_retry_replaces_partial_answer_and_persistence_is_throttled():
    values = []
    clock = [1.0]
    stream = AnswerStream(SimpleNamespace(progress=lambda kind, key, **data: values.append((key, data))), clock=lambda: clock[0])
    def delta(identifier, content, finish=None):
        stream({'id': identifier, 'choices': [{'delta': {'content': content}, 'finish_reason': finish}]})
    delta('attempt-1', 'old partial')
    delta('attempt-1', ' tail')
    assert len(values) == 1
    clock[0] += 0.3
    delta('attempt-2', 'replacement')
    assert values[-1][0] == values[0][0]
    assert values[-1][1]['text'] == 'replacement'
    delta('attempt-2', ' final', 'stop')
    assert values[-1][1]['text'] == 'replacement final'


def test_answer_limit_and_empty_usage_chunks():
    values = []
    stream = AnswerStream(SimpleNamespace(progress=lambda *args, **data: values.append(data)))
    stream({'choices': []})
    stream({'choices': [{'delta': {'content': 'x' * 50000}, 'finish_reason': 'stop'}]})
    assert len(values[0]['text']) == 32768


def test_progress_is_encrypted_replaceable_and_owner_scoped(workspace):
    client, headers, base = workspace
    task = create(workspace)
    with client.session_factory.begin() as db:
        db.get(AgentTask, task.id).status = 'running'
    tools = TaskTools(client.session_factory, task.id, None, get_settings(), None)
    tools.progress('assistant', 'response-1', text='部分回复')
    first = client.get(base + '/tasks/' + task.id, headers=headers).json()
    tools.progress('assistant', 'response-1', text='部分回复，继续输出')
    second = client.get(base + '/tasks/' + task.id, headers=headers).json()
    assert len(first['events']) == len(second['events'])
    assert second['events'][-1]['sequence'] == first['events'][-1]['sequence']
    assert second['events'][-1]['text'] == '部分回复，继续输出'
    assert second['updated_at'] > first['updated_at']
    with client.session_factory() as db:
        stored = db.get(AgentTask, task.id)
        assert '部分回复' not in stored.encrypted_events
        assert unseal(stored.encrypted_events, get_settings())[-1]['text'] == second['events'][-1]['text']
    assert client.get(base + '/tasks/' + task.id, headers={**headers,'X-CodeChat-Subject':'bob'}).status_code == 404


def test_live_logs_only_read_owned_pods_and_keep_last_output_on_timeout(workspace):
    client, headers, base = workspace
    task = create(workspace)
    with client.session_factory.begin() as db:
        db.get(AgentTask, task.id).status = 'running'
    read = []
    owner = ['expected-job']
    def logs(path, **kwargs):
        read.append(path)
        if len(read) > 1:
            raise httpx.ReadTimeout('synthetic timeout')
        return httpx.Response(200, text='step one; token=synthetic-secret; tail=synthetic-sec')
    cluster = SimpleNamespace(client=SimpleNamespace(get=logs), get=lambda path: {'items': [
        {'metadata': {'name':'command-pod','ownerReferences':[{'uid':owner[0],'kind':'Job'}]},'status':{'phase':'Running'}},
    ]})
    tools = TaskTools(client.session_factory, task.id, cluster, get_settings(), None)
    record = {'namespace':'test','name':'job','operation_id':'operation'}
    tools.command_progress(record, 'expected-job', ['synthetic-secret'])
    first = client.get(base + '/tasks/' + task.id, headers=headers).json()['events'][-1]
    assert first['phase'] == 'running'
    assert 'step one' in first['output'] and '[redacted]' in first['output']
    assert 'synthetic-sec' not in first['output']
    tools.command_progress(record, 'expected-job', ['synthetic-secret'])
    assert client.get(base + '/tasks/' + task.id, headers=headers).json()['events'][-1] == first
    owner[0] = 'unrelated-job'
    tools.command_progress(record, 'expected-job', ['synthetic-secret'])
    assert len(read) == 2
