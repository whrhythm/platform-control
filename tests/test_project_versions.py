"""Real file service + owner API: source recovery without replacing live files."""
import json
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from control_plane.models import ProjectVersion, ProjectRestore, AuditEvent
from control_plane.settings import get_settings
from control_plane.workspace_file_service import create_app
from control_plane.workspace_checkpoints import capture, restore
from control_plane.workspace_files import WorkspaceFiles, FileVersionConflict, UploadTooLarge
from test_capacity import prepare_capacity

PREFIX = '/internal/v1/workspace/versions'


@pytest.fixture
def versions(harness, monkeypatch, tmp_path):
    client, _ = harness
    client, auth, env_id, _, _ = prepare_capacity(client, monkeypatch)
    monkeypatch.setenv('PLATFORM_PROJECT_VERSIONS_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    get_settings.cache_clear()
    root = tmp_path / 'files'; root.mkdir()
    project = root / 'project'; project.mkdir()
    (project / 'main.py').write_text('print("before")\n')
    (project / '.git').mkdir(); (project / '.git' / 'config').write_text('keep git')
    (project / '.venv').mkdir(); (project / '.venv' / 'dependency').write_text('excluded')
    token = 't' * 40
    with TestClient(create_app(str(root), token)) as files:
        def forward(db, principal, settings, method, endpoint, path, **kwargs):
            response = files.request(method, endpoint, params={'path': path, **kwargs.get('params', {})},
                headers={'Authorization': 'Bearer ' + token}, content=kwargs.get('body'))
            if response.status_code >= 400:
                raise HTTPException(response.status_code, 'File operation could not be completed')
            return response
        monkeypatch.setattr('control_plane.project_versions.call_files', forward)
        monkeypatch.setattr('control_plane.capacity.call_files', forward)
        yield client, auth, env_id, root, forward


def create(client, auth, key='checkpoint-key-0001'):
    response = client.post(PREFIX, headers={**auth, 'Idempotency-Key': key}, json={'path': 'project', 'name': 'before edit'})
    assert response.status_code == 201, response.text
    return response.json()


def test_lifecycle_diff_restore_new_folder_capacity_audit(versions):
    client, auth, env_id, root, _ = versions
    version = create(client, auth)
    assert create(client, auth)['id'] == version['id']
    assert version['excluded'] == ['.git', '.venv']
    with client.session_factory() as db:
        row = db.get(ProjectVersion, version['id'])
        assert b'print' not in row.encrypted_content
        assert 'data' not in row.manifest['entries']['main.py']
    listing = client.get(PREFIX, headers=auth, params={'path':'project'}).json()
    assert len(listing['versions']) == 1
    account = client.get('/internal/v1/workspace/capacity', headers=auth).json()
    stored = next(item for item in account['storage']['components'] if item['component']=='versions')
    assert stored['used_bytes'] == version['stored_bytes'] > 0
    (root / 'project' / 'main.py').write_text('print("after")\n')
    diff = client.get(PREFIX + '/' + version['id'] + '/diff', headers=auth)
    assert diff.status_code == 200, diff.text
    assert '-print("before")' in diff.json()['changes'][0]['diff']
    url = PREFIX + '/' + version['id'] + '/restore'
    headers = {**auth, 'Idempotency-Key': 'restore-key-0000001'}
    response = client.post(url, headers=headers, json={'target':'recovered'})
    assert response.status_code == 200, response.text
    assert client.post(url, headers=headers, json={'target':'recovered'}).json() == response.json()
    assert (root/'recovered'/'main.py').read_text() == 'print("before")\n'
    assert (root/'project'/'main.py').read_text() == 'print("after")\n'
    assert (root/'project'/'.git'/'config').read_text() == 'keep git'
    assert not (root/'recovered'/'.venv').exists()
    assert not list(root.glob('.codechat-upload-restore-*'))
    assert client.delete(PREFIX+'/'+version['id'],headers=auth).status_code == 200
    assert client.get(PREFIX,headers=auth,params={'path':'project'}).json()['used_bytes'] == 0
    with client.session_factory() as db:
        actions = list(db.scalars(select(AuditEvent.action).where(AuditEvent.action.like('versions.%'))))
        assert actions == ['versions.created','versions.restore_requested','versions.restored','versions.deleted']


def test_lost_response_retry_verifies_receipt_without_second_write(versions, monkeypatch):
    client, auth, _, root, forward = versions
    version = create(client, auth)
    calls=[]
    def flaky(*args, **kwargs):
        response=forward(*args,**kwargs)
        if args[4]=='/v1/checkpoint-restore':
            calls.append('write')
            raise HTTPException(503,'lost response')
        return response
    monkeypatch.setattr('control_plane.project_versions.call_files',flaky)
    url=PREFIX+'/'+version['id']+'/restore'
    headers={**auth,'Idempotency-Key':'restore-lost-00001'}
    first=client.post(url,headers=headers,json={'target':'recovered'})
    assert first.status_code==503 and first.json()['detail']=='versions_restore_uncertain'
    assert client.delete(PREFIX+'/'+version['id'],headers=auth).status_code==409
    assert client.post(url,headers=headers,json={'target':'recovered'}).json()['status']=='ready'
    assert calls==['write']
    assert (root/'recovered'/'main.py').is_file()


def test_owner_boundary_existing_target_and_quota(versions, monkeypatch):
    client, auth, env_id, root, forward=versions
    version=create(client,auth)
    other={**auth,'X-CodeChat-Subject':'bob'}
    assert client.post('/internal/v1/workspace/ensure',headers=other).status_code==200
    assert client.get(PREFIX+'/'+version['id']+'/diff',headers=other).status_code==404
    assert client.delete(PREFIX+'/'+version['id'],headers=other).status_code==404
    response=client.post(PREFIX+'/'+version['id']+'/restore',headers={**auth,'Idempotency-Key':'restore-existing-001'},json={'target':'project'})
    assert response.status_code==409
    assert (root/'project'/'main.py').read_text()=='print("before")\n'
    def full(*args,**kwargs):
        if args[4]=='/v1/usage':
            value=forward(*args,**kwargs).json();value['used_bytes']=1024**4
            return httpx.Response(200,json=value)
        return forward(*args,**kwargs)
    monkeypatch.setattr('control_plane.project_versions.call_files',full)
    response=client.post(PREFIX,headers={**auth,'Idempotency-Key':'checkpoint-full-001'},json={'path':'project','name':'full'})
    assert response.status_code==507
    with client.session_factory() as db:
        assert len(list(db.scalars(select(ProjectVersion))))==1


def test_changed_source_aborts_and_restore_traversal_cannot_touch_outside(tmp_path,monkeypatch):
    root=tmp_path/'files';root.mkdir();(root/'project').mkdir();(root/'project'/'a').write_text('old')
    files=WorkspaceFiles(str(root))
    original=files.list_directory;calls=0
    def mutate(path):
        nonlocal calls
        calls+=1
        if calls==2:(root/'project'/'a').write_text('new')
        return original(path)
    monkeypatch.setattr(files,'list_directory',mutate)
    with pytest.raises(FileVersionConflict):capture(files,'project')
    monkeypatch.setattr(files,'list_directory',original)
    payload=capture(files,'project')
    payload['entries']['../escape']=payload['entries'].pop('a')
    with pytest.raises(ValueError):restore(files,'target',payload,str(uuid4()),'s'*40)
    assert not (tmp_path/'escape').exists() and not (root/'target').exists()


def test_metadata_limit_prevents_saving_an_unrestorable_point(tmp_path,monkeypatch):
    (tmp_path/'project').mkdir();(tmp_path/'project'/'a').write_text('small')
    monkeypatch.setattr('control_plane.workspace_checkpoints.MAX_PAYLOAD_BYTES',32)
    with pytest.raises(UploadTooLarge):capture(WorkspaceFiles(str(tmp_path)),'project')
