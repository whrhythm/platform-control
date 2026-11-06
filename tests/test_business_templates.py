from uuid import uuid4
import hashlib
from fastapi import HTTPException
from sqlalchemy import select

from control_plane.models import BusinessTemplate, ProjectDatabase, UserApplication, WorkspaceProject
from control_plane.settings import get_settings
from test_project_versions import versions

PREFIX='/internal/v1/workspace/business-templates'


def setup(versions, monkeypatch):
    client, auth, env_id, root, forward = versions
    monkeypatch.setenv('PLATFORM_BUSINESS_TEMPLATES_ENABLED','true')
    monkeypatch.setenv('PLATFORM_PROJECT_DATABASES_ENABLED','true')
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.business_templates.call_files',forward)
    monkeypatch.setattr('control_plane.project_databases.call_files',forward)
    return client, auth, env_id, root


def test_create_resume_preview_and_manual_source_checks(versions, monkeypatch):
    client, auth, env_id, root = setup(versions,monkeypatch)
    body={'request_id':str(uuid4()),'name':'售后测试'}
    initial=client.post(PREFIX,headers=auth,json=body); assert initial.status_code==201,initial.text
    value=initial.json(); identifier=value['project']['id']; path=value['project']['path']
    assert client.post(PREFIX,headers=auth,json=body).json()['project']['id']==identifier
    assert client.post(PREFIX+'/'+identifier+'/prepare',headers=auth).status_code==200
    original=(root/path/'app.py').read_bytes()
    again=client.post(PREFIX+'/'+identifier+'/prepare',headers=auth)
    assert again.json()['database']['id']
    with client.session_factory() as db:
        assert len(list(db.scalars(select(ProjectDatabase).where(ProjectDatabase.environment_id==env_id))))==1
    preview=client.get(PREFIX+'/'+identifier+'/preview',headers=auth)
    assert preview.status_code==200,preview.text
    sample=preview.json()
    assert 'connect-src \'none\'' in sample['html'] and '__SUPPORT_PREVIEW__=true' in sample['html']
    assert 'owner_password_hash' not in sample['html']
    checks={'source_sha256':sample['source_sha256'],'checks':['register','assign','filter','preview_boundary'],'revision':0}
    assert client.put(PREFIX+'/'+identifier+'/acceptance',headers=auth,json=checks).status_code==200
    (root/path/'index.html').write_text((root/path/'index.html').read_text()+'\n<!--changed-->')
    assert client.put(PREFIX+'/'+identifier+'/acceptance',headers=auth,json=checks).status_code==409
    client.post(PREFIX+'/'+identifier+'/prepare',headers=auth)
    assert (root/path/'index.html').read_text().endswith('<!--changed-->')
    assert (root/path/'app.py').read_bytes()==original
    assert client.post(PREFIX+'/'+identifier+'/access',headers={**auth,'X-CodeChat-Subject':'foreign'}).status_code==404


def test_partial_file_creation_is_resumable_and_never_overwrites(versions,monkeypatch):
    client,auth,_,root=setup(versions,monkeypatch)
    value=client.post(PREFIX,headers=auth,json={'request_id':str(uuid4()),'name':'恢复测试'}).json()
    identifier=value['project']['id']; path=value['project']['path']
    forward=versions[-1];calls=[]
    def interrupted(*args,**kwargs):
        if args[3]=='PUT':
            calls.append(args[5])
            if len(calls)==2:raise HTTPException(503,'synthetic lost reply')
        return forward(*args,**kwargs)
    monkeypatch.setattr('control_plane.business_templates.call_files',interrupted)
    assert client.post(PREFIX+'/'+identifier+'/prepare',headers=auth).status_code==503
    monkeypatch.setattr('control_plane.business_templates.call_files',forward)
    (root/path/'app.py').write_text('user edits')
    assert client.post(PREFIX+'/'+identifier+'/prepare',headers=auth).status_code==409
    assert (root/path/'app.py').read_text()=='user edits'
