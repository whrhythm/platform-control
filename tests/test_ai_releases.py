from uuid import uuid4
import json
import jwt
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from control_plane.models import (UserEnvironment, AIConfiguration, ModelCredential, AIAppCall, AIAppRun, AIRelease)
from control_plane.settings import get_settings
from control_plane.ai_sponsor import admission, settle
from test_ai_trials import initialized
from test_ai_projects import OWNER, PREFIX
from test_project_versions import versions


def prepared(versions, monkeypatch):
    monkeypatch.setenv('PLATFORM_AI_RELEASES_ENABLED','true')
    monkeypatch.setenv('PLATFORM_LITELLM_URL','http://model.test')
    client, auth, path, config, initial = initialized(versions, monkeypatch)
    get_settings.cache_clear()
    owner={'native_owner_id':OWNER}
    row=client.post(path+'/trials',headers=auth,json={**owner,'request_id':str(uuid4()),'expected_digest':initial['digest']}).json()
    target=path+'/trials/'+row['id']
    client.post(target+'/confirm',headers=auth,json=owner)
    run={**owner,'conversation_id':str(uuid4()),'message_id':str(uuid4())}
    client.post(target+'/resolve',headers=auth,json=run)
    accepted=client.post(target+'/acceptance',headers=auth,json={**run,'outcome':'passed','note':'verified',
        'transcript_digest':'a'*64,'answer_checked':True,'sources_checked':True})
    assert accepted.status_code==200,accepted.text
    publish={**owner,'request_id':str(uuid4()),'trial_id':row['id'],'expected_digest':initial['digest'],
        'budget_micros':100000,'confirm_sponsor':True}
    return client,auth,path,publish,config


def launched(client,auth,path,native=OWNER):
    response=client.post(path+'/launch',headers=auth,json={'native_owner_id':native})
    assert response.status_code==200,response.text
    entry=response.json()
    target=PREFIX+'/entries/'+entry['entry_id']
    assert client.post(target+'/confirm',headers=auth,json={'native_owner_id':native}).status_code==200
    body={'native_owner_id':native,'conversation_id':str(uuid4()),'message_id':str(uuid4())}
    response=client.post(target+'/resolve',headers=auth,json=body)
    assert response.status_code==200,response.text
    return entry,target,body,response.json()


def test_publication_is_fixed_idempotent_and_requires_current_acceptance(versions,monkeypatch):
    client,auth,path,body,config=prepared(versions,monkeypatch)
    assert client.post(path+'/releases',headers=auth,json={**body,'confirm_sponsor':False}).status_code==409
    first=client.post(path+'/releases',headers=auth,json=body)
    assert first.status_code==200,first.text
    assert client.post(path+'/releases',headers=auth,json=body).json()==first.json()
    entry,target,run,runtime=launched(client,auth,path)
    assert 'snapshot' not in entry and 'credential' not in entry
    changed=client.post(path+'/draft',headers=auth,json={'native_owner_id':OWNER,'expected_digest':body['expected_digest'],
        'config':{**config,'instructions':'changed draft'}})
    assert changed.status_code==200
    assert client.post(path+'/releases',headers=auth,json={**body,'request_id':str(uuid4())}).status_code==409
    after=client.post(target+'/resolve',headers=auth,json=run).json()
    assert after['snapshot']==runtime['snapshot']
    with client.session_factory() as db:assert len(list(db.scalars(select(AIRelease))))==1


def test_members_sessions_revocation_and_owner_only_management(versions,monkeypatch):
    client,auth,path,body,_=prepared(versions,monkeypatch)
    client.post(path+'/releases',headers=auth,json=body)
    with client.session_factory.begin() as db:
        owner=db.get(UserEnvironment,versions[2])
        from control_plane.litellm import identity_hash
        db.add(UserEnvironment(issuer=owner.issuer,subject='guest',identity_hash=identity_hash(owner.issuer,'guest'),storage_gib=1,
            first_tenant_id=owner.first_tenant_id,status='ready'))
    guest={**auth,'X-CodeChat-Subject':'guest'}
    assert client.post(path+'/launch',headers=guest,json={'native_owner_id':'b'*24}).status_code==403
    member={'native_owner_id':OWNER,'subject':'guest','native_id':'b'*24,'label':'guest@example.test','enabled':True}
    assert client.post(path+'/members',headers=auth,json=member).status_code==200
    assert client.get(path+'/releases',headers=guest).status_code==404
    entry,target,run,runtime=launched(client,guest,path,'b'*24)
    assert runtime['payer']=='creator'
    assert client.post(target+'/resolve',headers=auth,json={**run,'native_owner_id':OWNER}).status_code==403
    assert client.post(path+'/members/revoke',headers=auth,json={'native_owner_id':OWNER,'native_id':'b'*24}).status_code==200
    assert client.post(target+'/resolve',headers=guest,json=run).status_code==403
    assert client.get(target+'/source?name=secret.md',headers=guest).status_code==403
    assert client.post(target+'/search',headers=guest,json={'native_owner_id':'b'*24,'query':'x'}).status_code==403


def sponsored(versions,monkeypatch):
    client,auth,path,body,_=prepared(versions,monkeypatch)
    client.post(path+'/releases',headers=auth,json=body)
    _,_,_,runtime=launched(client,auth,path)
    settings=get_settings()
    claims=jwt.decode(runtime['credential'][6:],settings.librechat_service_token,algorithms=['HS256'],audience='ai-application')
    with client.session_factory.begin() as db:
        env=db.get(UserEnvironment,versions[2])
        from control_plane.ai_policy import effective_policy
        config=effective_policy(db,settings)
        db.add(AIConfiguration(id='default',config=config,catalog=[{'name':'test-model','input_cost_per_token':0.000001,'output_cost_per_token':0.000002}]))
        db.add(ModelCredential(issuer=env.issuer,subject=env.subject,status='ready',
            encrypted_key=Fernet(settings.model_credential_encryption_key.encode()).encrypt(b'sk-synthetic-owner').decode()))
    payload={'model':'test-model','stream':True,'messages':[{'role':'user','content':'question'}]}
    return client,claims,payload,runtime


def test_budget_ledger_duplicate_unknown_and_settlement(versions,monkeypatch):
    client,claims,payload,_=sponsored(versions,monkeypatch)
    with client.session_factory() as db:
        identifier,engine,key,out=admission(db,claims,payload,get_settings())
        assert key=='sk-synthetic-owner'
        assert out['metadata']['purpose']=='published_application'
        with pytest.raises(HTTPException) as duplicate:admission(db,claims,payload,get_settings())
        assert duplicate.value.status_code==409
    settle(engine,identifier,{'prompt_tokens':10,'completion_tokens':20},completed=True)
    with client.session_factory() as db:
        row=db.get(AIAppCall,identifier)
        assert row.state=='completed' and row.held_micros==50 and row.reserved_micros>50
        second,engine,_,_=admission(db,claims,{**payload,'messages':[{'role':'user','content':'second'}]},get_settings())
    settle(engine,second,None)
    with client.session_factory() as db:
        row=db.get(AIAppCall,second)
        assert row.state=='uncertain' and row.held_micros==row.reserved_micros
    settle(engine,second,{'prompt_tokens':0,'completion_tokens':0},completed=True)
    with client.session_factory() as db:assert db.get(AIAppCall,second).state=='uncertain'


def test_sponsor_http_stream_uses_creator_key_strips_reasoning_and_stops_replay(versions,monkeypatch):
    client,claims,payload,runtime=sponsored(versions,monkeypatch)
    seen=[]
    def response(request):
        seen.append(request)
        assert request.headers['authorization']=='Bearer sk-synthetic-owner'
        outgoing=json.loads(request.content)
        assert outgoing['metadata']['app_call_id']
        assert outgoing['metadata']['spend_logs_metadata']['app_call_id'] == outgoing['metadata']['app_call_id']
        return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':{'content':'回答','reasoning_content':'private'}}]})+
            '\n\ndata: '+json.dumps({'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':20}})+'\n\ndata: [DONE]\n\n')
    original=httpx.AsyncClient
    monkeypatch.setattr('control_plane.ai_sponsor.httpx.AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(response),**kwargs))
    auth={'Authorization':'Bearer '+runtime['credential']}
    first=client.post('/agent/app/v1/chat/completions',headers=auth,json=payload)
    assert first.status_code==200 and '回答' in first.text and 'private' not in first.text
    assert client.post('/agent/app/v1/chat/completions',headers=auth,json=payload).status_code==409
    assert len(seen)==1


@pytest.mark.parametrize('status,state,held', [(429,'rejected',False),(422,'rejected',False),(503,'uncertain',True),(200,'uncertain',True)])
def test_upstream_failure_retains_unknown_cost_without_replay(versions,monkeypatch,status,state,held):
    client,_,payload,runtime=sponsored(versions,monkeypatch)
    seen=[]
    def response(request):
        seen.append(request)
        # Even a 200 response without a final receipt cannot prove completion.
        return httpx.Response(status,text='data: '+json.dumps({'choices':[{'delta':{'content':'partial'}}]})+'\n\n')
    original=httpx.AsyncClient
    monkeypatch.setattr('control_plane.ai_sponsor.httpx.AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(response),**kwargs))
    auth={'Authorization':'Bearer '+runtime['credential']}
    client.post('/agent/app/v1/chat/completions',headers=auth,json=payload)
    assert client.post('/agent/app/v1/chat/completions',headers=auth,json=payload).status_code==409
    assert len(seen)==1
    with client.session_factory() as db:
        row=db.scalar(select(AIAppCall))
        assert row.state==state and bool(row.held_micros)==held


def test_application_budget_is_terminal_before_any_provider_request(versions,monkeypatch):
    client,_,payload,runtime=sponsored(versions,monkeypatch)
    from control_plane.models import AIApplication
    with client.session_factory.begin() as db:
        db.scalar(select(AIApplication)).budget_micros=1000
    def forbidden(**kwargs):
        pytest.fail('Insufficient application budget must never contact the provider')
    monkeypatch.setattr('control_plane.ai_sponsor.httpx.AsyncClient',forbidden)
    response=client.post('/agent/app/v1/chat/completions',headers={'Authorization':'Bearer '+runtime['credential']},json=payload)
    assert response.status_code==402
    assert response.headers['x-should-retry']=='false'
    assert response.json()['error']['code']=='ai_releases_budget_exhausted'
    with client.session_factory() as db:assert list(db.scalars(select(AIAppCall)))==[]
