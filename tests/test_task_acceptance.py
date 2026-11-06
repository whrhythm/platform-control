from uuid import uuid4

from sqlalchemy import select

from control_plane.agent_tasks import submit_task, unseal
from control_plane.agent_execution import TaskTools
from control_plane.auth import Principal
from control_plane.models import AgentTask, AuditEvent
from control_plane.settings import get_settings
from control_plane.task_acceptance import original_request
from test_agent_workbench import workspace, create


def setup_record(workspace, status='completed'):
    client, headers, base = workspace
    task = create(workspace)
    with client.session_factory.begin() as db:
        db.get(AgentTask, task.id).status = status
    return task, base + '/tasks/' + task.id + '/acceptance'


def body():
    return {'revision': 0, 'goal': '新增中文导出', 'criteria': [
        {'id': str(uuid4()), 'text': '导出保留中文', 'result': 'pending'}], 'note': ''}


def test_acceptance_owner_encryption_persistence_and_audit(workspace):
    client, headers, _ = workspace
    task, url = setup_record(workspace)
    assert client.get(url, headers=headers).json()['request'] is None
    value = body()
    saved = client.put(url, headers=headers, json=value)
    assert saved.status_code == 200
    assert saved.json()['revision'] == 1
    assert client.get(url, headers=headers).json() == saved.json()
    other = {**headers, 'X-CodeChat-Subject': 'bob'}
    assert client.get(url, headers=other).status_code == 404
    assert client.put(url, headers=other, json=value).status_code == 404
    with client.session_factory() as db:
        stored = db.get(AgentTask, task.id)
        assert value['goal'] not in str(stored.resources)
        assert unseal(stored.resources['acceptance']['encrypted'], get_settings())['goal'] == value['goal']
        audit = db.scalars(select(AuditEvent).where(AuditEvent.action == 'agent.acceptance.saved')).all()
        assert len(audit) == 1 and audit[0].target_id == task.id + '/1'
        assert value['goal'] not in str(audit[0].__dict__)


def test_acceptance_lost_response_retry_and_stale_writer(workspace):
    client, headers, _ = workspace
    task, url = setup_record(workspace)
    value = body()
    first = client.put(url, headers=headers, json=value).json()
    assert client.put(url, headers=headers, json=value).json() == first
    assert client.put(url, headers=headers, json={**value, 'note': 'other tab'}).status_code == 409
    changed = {**value, 'revision': 1, 'criteria': [{**value['criteria'][0], 'result': 'passed'}]}
    assert client.put(url, headers=headers, json=changed).json()['revision'] == 2
    record = client.get(url, headers=headers).json()
    assert record['criteria'][0]['result'] == 'passed'
    with client.session_factory() as db:
        assert db.get(AgentTask, task.id).status == 'completed'


def test_modified_requirements_reset_results_and_active_task_cannot_pass(workspace):
    client, headers, _ = workspace
    task, url = setup_record(workspace, 'running')
    value = body()
    assert client.put(url, headers=headers, json=value).status_code == 200
    passed = {**value, 'revision': 1, 'criteria': [{**value['criteria'][0], 'result': 'passed'}]}
    assert client.put(url, headers=headers, json=passed).status_code == 409
    with client.session_factory.begin() as db:
        db.get(AgentTask, task.id).status = 'completed'
    assert client.put(url, headers=headers, json=passed).status_code == 200
    assert client.put(url, headers=headers, json={**passed, 'revision': 2, 'goal': 'different goal'}).status_code == 409
    pending = {**value, 'revision': 2, 'goal': 'different goal'}
    assert client.put(url, headers=headers, json=pending).json()['criteria'][0]['result'] == 'pending'


def test_request_capture_never_exposes_system_or_previous_messages(workspace):
    client, headers, base = workspace
    messages = [{'role': 'system', 'content': 'private system'}, {'role': 'user', 'content': 'old request'},
                {'role': 'user', 'content': [{'type': 'text', 'text': 'new requirement'}]}]
    with client.session_factory() as db:
        task = submit_task(db, Principal(get_settings().oidc_issuer, 'alice'), get_settings(),
                           'conversation', 'private system and full history', 'model-one',
                           user_request=original_request(messages))
    result = client.get(base + '/tasks/' + task.id + '/acceptance', headers=headers).json()
    assert result['request'] == {'text': 'new requirement', 'truncated': False}
    assert result['goal'] == 'new requirement'
    assert 'private system' not in str(result) and 'old request' not in str(result)
    assert original_request([{'content': 'a' * 4001}]) == {'text': 'a' * 4000, 'truncated': True}


def test_invalid_inputs_are_bounded_and_do_not_echo_private_text(workspace):
    client, headers, _ = workspace
    _, url = setup_record(workspace)
    value = body()
    for invalid in ({**value, 'owner': 'bob'}, {**value, 'goal': 'secret' * 1000},
                    {**value, 'criteria': value['criteria'] * 2},
                    {**value, 'criteria': [{**value['criteria'][0], 'text': ' '}]}):
        response = client.put(url, headers=headers, json=invalid)
        assert response.status_code == 422
        assert response.json() == {'detail': 'invalid_acceptance_input'}


def test_execution_progress_preserves_review_and_next_task_does_not_inherit_it(workspace):
    client, headers, base = workspace
    task, url = setup_record(workspace, 'running')
    assert client.put(url, headers=headers, json=body()).status_code == 200
    tools = TaskTools(client.session_factory, task.id, None, get_settings(), None)
    tools.event('file', operation='write', path='app.py')
    assert client.get(url, headers=headers).json()['revision'] == 1
    with client.session_factory.begin() as db:
        db.get(AgentTask, task.id).status = 'completed'
    next_task = create(workspace, 'another requirement')
    result = client.get(base + '/tasks/' + next_task.id + '/acceptance', headers=headers).json()
    assert result['revision'] == 0 and result['criteria'] == []
