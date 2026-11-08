from datetime import timedelta
from uuid import uuid4
import pytest

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


@pytest.fixture(autouse=True)
def forward_workspace_requests(versions, monkeypatch):
    monkeypatch.setattr('control_plane.ai_project_deletion.call_files', versions[4])


def confirm(plan):
    return {'confirmation': plan['confirmation'], 'remove_folder': bool(plan['folder_cleanup'])}


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
    started = client.post(url, headers=auth, json={**owner, **confirm(plan)})
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
    started = client.post(url, headers=auth, json={**owner, **confirm(plan)})
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


def test_project_created_library_is_preserved_when_another_project_uses_it(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    base = PREFIX + '/' + project
    owner = {'native_owner_id': OWNER}
    revision = client.post(base + '/knowledge', headers=auth, json={
        **owner, 'request_id': str(uuid4()), 'files': [{'name': 'shared.md', 'content': 'shared source'}],
    }).json()['id']
    store = Store()
    assert run_once(client.session_factory, get_settings(), store)
    second = client.post(PREFIX, headers=auth, json={
        **owner, 'request_id': str(uuid4()), 'name': 'Second assistant', 'purpose': 'Use shared knowledge',
    }).json()
    second_path = PREFIX + '/' + second['project']['id']
    assert client.post(second_path + '/confirm', headers=auth, json={
        **owner, 'agent_id': second['agent_id'],
    }).status_code == 200
    assert client.post(second_path + '/knowledge-library', headers=auth, json={
        **owner, 'library_id': project, 'expected_library_id': None,
    }).status_code == 200
    url = base + '/deletion'
    plan = client.get(url, headers=auth).json()
    assert (plan['knowledge_libraries'], plan['knowledge_revisions'], plan['preserved_libraries']) == (0, 0, 1)
    assert client.post(url, headers=auth, json={**owner, **confirm(plan)}).json()['state'] == 'native'
    assert client.post(url + '/complete', headers=auth, json=owner).status_code == 200
    detail = client.get('/internal/v1/workspace/knowledge-libraries/' + project, headers=auth).json()
    assert detail['active_revision_id'] == revision
    assert [row['id'] for row in detail['references']['projects']] == [second['project']['id']]


def test_root_scope_requires_reconfirmation_and_does_not_follow_symlinks(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    root = versions[3]
    folder = root / ('assistant-' + project.replace('-', '')[:16])
    (folder / 'sub').mkdir()
    (folder / 'sub' / 'owned.txt').write_text('owned')
    (folder / 'outside').symlink_to(root / 'project', target_is_directory=True)
    outside = (root / 'project' / 'main.py').read_bytes()
    url = PREFIX + '/' + project + '/deletion'
    owner = {'native_owner_id': OWNER}
    first = client.get(url, headers=auth).json()
    assert first['folder_cleanup']['files'] == 2 and first['folder_cleanup']['directories'] == 1
    (folder / 'late.txt').write_text('after preview')
    assert client.post(url, headers=auth, json={**owner, **confirm(first)}).status_code == 409
    second = client.get(url, headers=auth).json()
    assert second['folder_cleanup']['files'] == 3
    assert client.post(url, headers=auth, json={**owner, 'confirmation': second['confirmation']}).status_code == 409
    assert client.post(url, headers=auth, json={**owner, **confirm(second)}).status_code == 200
    assert client.post(url + '/complete', headers=auth, json=owner).json() == {'state': 'deleted'}
    assert not folder.exists()
    assert (root / 'project' / 'main.py').read_bytes() == outside


def test_legacy_and_changed_roots_are_preserved(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    owner = {'native_owner_id': OWNER}
    url = PREFIX + '/' + project + '/deletion'
    root = versions[3]
    folder = root / ('assistant-' + project.replace('-', '')[:16])
    (folder / 'own.txt').write_text('before')
    plan = client.get(url, headers=auth).json()
    assert plan['folder_cleanup']['files'] == 1
    assert client.post(url, headers=auth, json={**owner, **confirm(plan)}).status_code == 200
    (folder / 'own.txt').write_text('changed after confirmation')
    assert client.post(url + '/complete', headers=auth, json=owner).json() == {'state': 'deleted', 'folder_retained': True}
    assert (folder / 'own.txt').read_text() == 'changed after confirmation'

    native = client.post(PREFIX, headers=auth, json={**owner, 'name': 'Legacy',
        'purpose': 'Fixture', 'request_id': str(uuid4())}).json()
    other = native['project']['id']
    assert client.post(PREFIX + '/' + other + '/confirm', headers=auth, json={
        **owner, 'agent_id': native['agent_id'],
    }).status_code == 200
    from control_plane.models import WorkspaceProject
    with client.session_factory.begin() as db:
        db.get(WorkspaceProject, other).project_path = 'project'
    url = PREFIX + '/' + other + '/deletion'
    plan = client.get(url, headers=auth).json()
    assert plan['folder_cleanup'] is None
    assert client.post(url, headers=auth, json={**owner, **confirm(plan)}).status_code == 200
    assert client.post(url + '/complete', headers=auth, json=owner).json() == {'state': 'deleted'}
    assert (root / 'project' / 'main.py').is_file()


def test_staged_folder_retry_leaves_new_replacement_alone(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    from control_plane import project_root_cleanup
    owner = {'native_owner_id': OWNER}
    url = PREFIX + '/' + project + '/deletion'
    root = versions[3]
    plan = client.get(url, headers=auth).json()
    assert client.post(url, headers=auth, json={**owner, **confirm(plan)}).status_code == 200
    original = project_root_cleanup.shutil.rmtree
    with monkeypatch.context() as patch:
        def interrupted(*args, **kwargs):
            raise OSError('interrupted after staging')
        patch.setattr(project_root_cleanup.shutil, 'rmtree', interrupted)
        interrupted.avoids_symlink_attacks = original.avoids_symlink_attacks
        assert client.post(url + '/complete', headers=auth, json=owner).status_code == 503
    replacement = root / plan['folder']
    replacement.mkdir()
    (replacement / 'new.txt').write_text('keep')
    assert client.post(url + '/complete', headers=auth, json=owner).json() == {'state': 'deleted'}
    assert (replacement / 'new.txt').read_text() == 'keep'
    assert not list(root.glob('.codechat-upload-project-delete-*'))
