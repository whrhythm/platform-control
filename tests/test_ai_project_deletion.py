from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from control_plane.ai_sponsor import admission, settle
from control_plane.knowledge_worker import run_once
from control_plane.models import (AIAppCall, AIProjectBinding, AIProjectDraft, AIProjectTrial,
                                  AuditEvent, ProjectKnowledge, now)
from control_plane.settings import get_settings
from test_ai_projects import OWNER, PREFIX
from test_ai_releases import sponsored
from test_knowledge import Store, initialized
from test_knowledge_libraries import create
from test_project_versions import versions


def test_deletion_retries_index_cleanup_and_keeps_independent_library(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    project_path = PREFIX + '/' + project
    url = project_path + '/deletion'
    owner = {'native_owner_id': OWNER}
    before = client.get(url, headers=auth).json()
    uploaded = client.post(project_path + '/knowledge', headers=auth, json={
        **owner, 'request_id': str(uuid4()), 'files': [{'name': 'policy.md', 'content': 'sample source'}],
    })
    assert uploaded.status_code == 202, uploaded.text
    revision = uploaded.json()['id']
    store = Store()
    assert run_once(client.session_factory, get_settings(), store)
    library = create(client, auth)
    assert client.post(project_path + '/knowledge-library', headers=auth, json={
        **owner, 'library_id': library['id'], 'expected_library_id': project,
    }).status_code == 200
    plan = client.get(url, headers=auth).json()
    assert (plan['knowledge_libraries'], plan['knowledge_revisions'], plan['preserved_libraries']) == (1, 1, 1)
    assert client.post(url, headers=auth, json={**owner, 'confirmation': before['confirmation']}).status_code == 409
    assert client.post(url, headers=auth, json={**owner, 'native_owner_id': 'b' * 24,
        'confirmation': plan['confirmation']}).status_code == 404
    assert client.get(url, headers={**auth, 'X-CodeChat-Subject': 'foreign'}).status_code == 404
    started = client.post(url, headers=auth, json={**owner, 'confirmation': plan['confirmation']})
    assert started.json() == {'state': 'knowledge', 'remaining': 1, 'retrying': False}
    assert client.post(url + '/complete', headers=auth, json=owner).status_code == 409
    assert client.get(project_path + '/knowledge', headers=auth).status_code == 404

    class Offline:
        def call(self, *args):
            raise OSError('index service unavailable')

    assert run_once(client.session_factory, get_settings(), Offline())
    assert client.post(url, headers=auth, json=owner).json()['retrying'] is True
    with client.session_factory.begin() as db:
        db.get(ProjectKnowledge, revision).lease_until = now() - timedelta(seconds=1)
    assert run_once(client.session_factory, get_settings(), store)
    assert client.get(url, headers=auth).json() == {'state': 'native'}
    assert len(client.post(url, headers=auth, json=owner).json()['agents']) == 1
    for _ in range(2):
        assert client.post(url + '/complete', headers=auth, json=owner).json() == {'state': 'deleted'}
    assert client.get(PREFIX, headers=auth).json()['projects'] == []
    assert client.get('/internal/v1/workspace/projects/' + project, headers=auth).status_code == 404
    libraries = client.get('/internal/v1/workspace/knowledge-libraries', headers=auth).json()['libraries']
    assert [row['id'] for row in libraries] == [library['id']]
    with client.session_factory() as db:
        assert db.get(ProjectKnowledge, revision).encrypted_source == ''
        assert db.get(AIProjectBinding, project).state == 'deleted'
        assert list(db.scalars(select(AuditEvent.action).where(AuditEvent.action.like('ai_project.deletion_%')))) == [
            'ai_project.deletion_started', 'ai_project.deletion_completed']


def test_deletion_revokes_runtime_and_preserves_billing_including_inflight(versions, monkeypatch):
    client, claims, payload, runtime = sponsored(versions, monkeypatch)
    with client.session_factory() as db:
        call_id, engine, _, _ = admission(db, claims, payload, get_settings())
        project = db.get(AIAppCall, call_id).project_id
    url = PREFIX + '/' + project + '/deletion'
    auth = versions[1]
    owner = {'native_owner_id': OWNER}
    plan = client.get(url, headers=auth).json()
    assert (plan['trials'], plan['releases']) == (1, 1)
    started = client.post(url, headers=auth, json={**owner, 'confirmation': plan['confirmation']})
    assert started.json()['state'] == 'native'
    assert client.post('/agent/app/v1/chat/completions',
        headers={'Authorization': 'Bearer ' + runtime['credential']}, json=payload).status_code == 403
    assert client.post(url + '/complete', headers=auth, json=owner).status_code == 200
    settle(engine, call_id, {'prompt_tokens': 3, 'completion_tokens': 7}, completed=True)
    with client.session_factory() as db:
        assert db.get(AIAppCall, call_id).held_micros == 17
        assert db.get(AIProjectDraft, project) is None
        trial = db.scalar(select(AIProjectTrial).where(AIProjectTrial.project_id == project))
        assert trial.encrypted_snapshot == '' and trial.knowledge_revision_id is None
