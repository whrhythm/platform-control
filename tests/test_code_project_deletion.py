from uuid import uuid4

from control_plane.models import CodeProjectDeletion, ProjectDatabase, UserApplication, WorkspaceProject
from control_plane.project_databases import names
from test_ai_projects import OWNER
from test_project_versions import versions

PREFIX = '/internal/v1/workspace/projects'
OWNER_BODY = {'native_owner_id': OWNER}


def create_project(versions, monkeypatch):
    client, auth, env_id, root, forward = versions
    monkeypatch.setattr('control_plane.workspace_projects.call_files', forward)
    monkeypatch.setattr('control_plane.ai_project_deletion.call_files', forward)
    body = {'path': 'retirement-test', 'name': 'Disposable', 'goal': '', 'revision': 0, 'request_id': str(uuid4())}
    response = client.put(PREFIX, headers=auth, json=body)
    assert response.status_code == 200, response.text
    return client, auth, env_id, root / body['path'], response.json()['id']


def confirm(client, auth, url, plan):
    return client.post(url, headers=auth, json={**OWNER_BODY, 'confirm_code': True,
        'confirmation': plan['confirmation'], 'remove_folder': bool(plan['folder_cleanup'])})


def test_deployment_database_retry_and_directory_follow_confirmed_plan(versions, monkeypatch):
    client, auth, env_id, folder, project = create_project(versions, monkeypatch)
    (folder / 'source.py').write_text('print(1)')
    image = 'harbor.local/private/app@sha256:' + 'a'*64
    with client.session_factory.begin() as db:
        app = UserApplication(environment_id=env_id, project_path=folder.name,
            status='running', desired_state='running', image=image, url='https://app.example.test/')
        database = ProjectDatabase(environment_id=env_id, project_path=folder.name,
            status='ready', encrypted_password='synthetic', reserved_bytes=1)
        db.add_all((app, database))
        db.flush()
        app_id, db_id = app.id, database.id
    url = PREFIX + '/' + project + '/deletion'
    plan = client.get(url, headers=auth).json()
    assert plan['code'] == {'deployment': True, 'domain': 'app.example.test', 'database': 'delete',
                            'images': [image], 'image_repository': None}
    assert plan['folder_cleanup']['files'] == 1
    assert client.post(url, headers=auth, json={**OWNER_BODY, 'confirmation': plan['confirmation'],
        'remove_folder': True}).status_code == 409
    assert client.get(url, headers={**auth, 'X-CodeChat-Subject': 'foreign'}).status_code == 404
    assert confirm(client, auth, url, plan).json() == {'state': 'deployment'}
    assert client.post(url + '/complete', headers=auth, json=OWNER_BODY).status_code == 409
    with client.session_factory.begin() as db:
        assert db.get(UserApplication, app_id).desired_state == 'deleted'
        assert db.get(ProjectDatabase, db_id).status == 'ready'
        db.get(UserApplication, app_id).status = 'deleted'
    assert client.post(url, headers=auth, json=OWNER_BODY).json() == {'state': 'database'}
    with client.session_factory.begin() as db:
        row = db.get(ProjectDatabase, db_id)
        assert row.status == 'deleting'
        row.status, row.error_code = 'failed', 'database_operation_failed'
    assert client.post(url, headers=auth, json=OWNER_BODY).status_code == 503
    assert client.post(url, headers=auth, json={**OWNER_BODY, 'retry_failed': True}).json() == {'state': 'database'}
    with client.session_factory.begin() as db:
        assert db.get(ProjectDatabase, db_id).status == 'deleting'
        db.get(ProjectDatabase, db_id).status = 'deleted'
    assert client.post(url, headers=auth, json=OWNER_BODY).json() == {'state': 'native'}
    assert client.post(url + '/complete', headers=auth, json=OWNER_BODY).json() == {'state': 'deleted'}
    assert client.post(url + '/complete', headers=auth, json=OWNER_BODY).json() == {'state': 'deleted'}
    assert not folder.exists()
    assert client.get(PREFIX, headers=auth).json()['projects'] == []
    with client.session_factory() as db:
        assert db.get(CodeProjectDeletion, project).completed
        assert db.get(UserApplication, app_id).image == image


def test_preserves_a_shared_database_and_a_legacy_folder(versions, monkeypatch):
    client, auth, env_id, folder, project = create_project(versions, monkeypatch)
    original = versions[3] / 'project' / 'main.py'
    source = original.read_bytes()
    with client.session_factory.begin() as db:
        database = ProjectDatabase(environment_id=env_id, project_path=folder.name,
            status='ready', encrypted_password='synthetic', reserved_bytes=1)
        db.add(database)
        db.flush()
        db.add(UserApplication(environment_id=env_id, project_path='another-project', status='running',
            desired_state='running', resources={'database_secret': names(database)[2]}))
        legacy = WorkspaceProject(environment_id=env_id, project_path='project', name='Legacy',
                                  encrypted_goal=db.get(WorkspaceProject, project).encrypted_goal)
        db.add(legacy)
        db.flush()
        db_id, legacy_id = database.id, legacy.id
    url = PREFIX + '/' + project + '/deletion'
    plan = client.get(url, headers=auth).json()
    assert plan['code']['database'] == 'keep'
    assert plan['folder_cleanup'] is None
    assert confirm(client, auth, url, plan).json()['state'] == 'native'
    assert client.post(url + '/complete', headers=auth, json=OWNER_BODY).json()['state'] == 'deleted'
    assert folder.is_dir()
    with client.session_factory() as db:
        assert db.get(ProjectDatabase, db_id).status == 'ready'
    legacy_url = PREFIX + '/' + legacy_id + '/deletion'
    legacy_plan = client.get(legacy_url, headers=auth).json()
    assert legacy_plan['folder_cleanup'] is None
    assert confirm(client, auth, legacy_url, legacy_plan).json()['state'] == 'native'
    assert client.post(legacy_url + '/complete', headers=auth, json=OWNER_BODY).json()['state'] == 'deleted'
    assert original.read_bytes() == source
