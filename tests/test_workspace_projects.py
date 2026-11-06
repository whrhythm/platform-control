import hashlib
from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select
from control_plane.agent_tasks import seal
from control_plane.models import AgentTask, AuditEvent, UserApplication, UserEnvironment, WorkspaceProject
from control_plane.settings import get_settings
from test_project_versions import versions

PREFIX = '/internal/v1/workspace/projects'


@pytest.fixture
def projects(versions, monkeypatch):
    client, auth, env_id, root, forward = versions
    monkeypatch.setattr('control_plane.workspace_projects.call_files', forward)
    return client, auth, env_id, root


def save(client, auth, **changes):
    return client.put(PREFIX, headers=auth, json={'path': 'project', 'name': 'My project',
        'goal': 'Persist work', 'revision': 0, **changes})


def test_associate_retry_update_conflict_preserves_files(projects):
    client, auth, env_id, root = projects
    original = (root/'project'/'main.py').read_bytes()
    first = save(client, auth); assert first.status_code == 200, first.text
    value = first.json()
    assert save(client, auth).json() == value
    assert save(client, auth, goal='different').status_code == 409
    assert save(client, auth, goal='changed', revision=1).json()['revision'] == 2
    assert save(client, auth, goal='changed', revision=1).json()['revision'] == 2
    assert client.get(PREFIX, headers=auth).json()['projects'][0]['goal'] == 'changed'
    assert (root/'project'/'main.py').read_bytes() == original
    with client.session_factory() as db:
        assert 'changed' not in db.get(WorkspaceProject, value['id']).encrypted_goal
        assert len(list(db.scalars(select(AuditEvent).where(AuditEvent.action=='project.context.saved')))) == 2
        assert not list(db.scalars(select(UserApplication)))


def test_context_scopes_tasks_and_source_comparison(projects):
    client, auth, env_id, root = projects
    identifier = save(client, auth).json()['id']; url = PREFIX+'/'+identifier
    settings = get_settings()
    with client.session_factory() as db:
        for path in ('project', 'unrelated'):
            db.add(AgentTask(environment_id=env_id, conversation_id='test-conversation', request_key=path,
                config={'project_path':path}, encrypted_prompt=seal('private prompt', settings),
                encrypted_events=seal([],settings), status='completed', resources={
                    'user_request': seal({'text':path+' goal','truncated':False},settings),
                    'acceptance':{'revision':1,'counts':{'total':1,'pending':1,'passed':0,'changes_requested':0}}}))
        db.commit()
    context = client.get(url, headers=auth).json()
    assert len(context['tasks']) == 1 and context['tasks'][0]['title'] == 'project goal'
    assert 'private prompt' not in str(context)
    assert context['application'] is None
    observed = client.get(url+'/source', headers=auth).json()
    assert observed['state'] == 'unbound' and len(observed['source_sha256']) == 64
    with client.session_factory() as db:
        db.add(UserApplication(environment_id=env_id, project_path='project',status='stopped',desired_state='stopped',
            resources={'last_good':{'provenance':{'revision':'release-one','source_sha256':observed['source_sha256']}}}))
        db.commit()
    assert client.get(url+'/source',headers=auth).json()['state']=='same'
    assert client.get(url,headers=auth).json()['application']['status']=='stopped'
    (root/'project'/'main.py').write_text('changed')
    changed=client.get(url+'/source', headers=auth).json()
    assert changed['source_sha256'] != observed['source_sha256'] and changed['state']=='changed'


def test_invalid_missing_path_and_foreign_project(projects):
    client, auth, env_id, _ = projects
    assert save(client, auth, path='../escape').status_code == 422
    assert save(client, auth, path='missing').status_code == 404
    identifier = save(client, auth).json()['id']
    with client.session_factory() as db:
        env = db.get(UserEnvironment,env_id)
        other = UserEnvironment(issuer=env.issuer, subject='foreign', identity_hash='f'*64,
            storage_gib=1, first_tenant_id=env.first_tenant_id, status='ready')
        db.add(other); db.flush()
        row = db.get(WorkspaceProject,identifier)
        row.environment_id = other.id
        db.commit()
    assert client.get(PREFIX+'/'+identifier,headers=auth).status_code == 404
    assert client.get(PREFIX+'/'+identifier+'/source',headers=auth).status_code == 404


def test_source_failure_does_not_remove_project(projects,monkeypatch):
    from fastapi import HTTPException
    client,auth,_,_=projects
    identifier=save(client,auth).json()['id']
    def fail(*args,**kwargs):raise HTTPException(503,'secret provider error')
    monkeypatch.setattr('control_plane.workspace_projects.call_files',fail)
    response=client.get(PREFIX+'/'+identifier+'/source',headers=auth)
    assert response.status_code==503 and 'secret' not in response.text
    assert client.get(PREFIX+'/'+identifier,headers=auth).status_code==200
