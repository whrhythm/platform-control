from datetime import timedelta
from uuid import uuid4
import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from control_plane.models import UserEnvironment, AIAppMember, AIAppCall, AIAppEntry, AIAppRun, AIApplication, now
from control_plane.ai_sponsor import admission, settle
from control_plane.settings import get_settings
from test_ai_releases import prepared, launched, sponsored
from test_ai_projects import OWNER, PREFIX
from test_project_versions import versions


def guest(client, auth, env_id):
    from control_plane.litellm import identity_hash
    with client.session_factory.begin() as db:
        owner = db.get(UserEnvironment, env_id)
        row = UserEnvironment(issuer=owner.issuer, subject='sharing-guest',
            identity_hash=identity_hash(owner.issuer,'sharing-guest'), storage_gib=1,
            first_tenant_id=owner.first_tenant_id, status='ready')
        db.add(row); db.flush()
        identifier = row.id
    return {**auth,'X-CodeChat-Subject':'sharing-guest'}, identifier


def invite(client, auth, path, **kwargs):
    return client.post(path+'/members',headers=auth,json={'native_owner_id':OWNER,
        'subject':'sharing-guest','native_id':'b'*24,'label':'synthetic@example.test','enabled':True,**kwargs})


def reply(client, auth, path, revision=1, accept=True):
    return client.post(path+'/invitation',headers=auth,json={'native_owner_id':'b'*24,
        'expected_revision':revision,'accept':accept})


def test_catalog_invitation_accept_revoke_reinvite_and_expiry(versions, monkeypatch):
    client, auth, path, body, _ = prepared(versions, monkeypatch)
    other, env_id = guest(client, auth, versions[2])
    assert client.get(PREFIX+'/catalog',headers=auth).json() == {'items':[],'invitations':[]}
    client.post(path+'/releases',headers=auth,json=body)
    mine = client.get(PREFIX+'/catalog',headers=auth).json()['items']
    assert len(mine)==1 and mine[0]['kind']=='mine'
    assert not client.get(PREFIX+'/catalog',headers=other).json()['items']
    first = invite(client,auth,path,budget_micros=20000)
    assert first.status_code==200,first.text
    assert invite(client,auth,path,budget_micros=99999).json()==first.json()
    assert first.json()['members'][0]['state']=='pending'
    pending = client.get(PREFIX+'/catalog',headers=other).json()
    assert len(pending['invitations'])==1 and not pending['items']
    assert 'snapshot' not in str(pending) and 'encrypted' not in str(pending)
    assert client.post(path+'/launch',headers=other,json={'native_owner_id':'b'*24}).status_code==403
    assert reply(client,other,path).status_code==200
    assert reply(client,other,path).status_code==200
    assert client.get(PREFIX+'/catalog',headers=other).json()['items'][0]['budget_micros']==20000
    entry, target, run, _ = launched(client,other,path,'b'*24)
    for suffix in ('/members/policy','/access'):
        payload = {'native_owner_id':'b'*24,'expected_revision':1,'budget_micros':50000}
        payload.update({'native_id':'b'*24} if suffix.endswith('policy') else {'enabled':True})
        assert client.post(path+suffix,headers=other,json=payload).status_code==404
    revoke = client.post(path+'/members/revoke',headers=auth,json={'native_owner_id':OWNER,'native_id':'b'*24})
    rev = revoke.json()['members'][0]['revision']
    assert not client.get(PREFIX+'/catalog',headers=other).json()['items']
    assert client.post(target+'/resolve',headers=other,json=run).status_code==403
    assert reply(client,other,path).status_code==409
    assert invite(client,auth,path).status_code==409
    assert invite(client,auth,path,expected_revision=rev).status_code==200
    assert reply(client,other,path,rev+1,False).status_code==200
    assert reply(client,other,path,rev+1).status_code==409
    assert invite(client,auth,path,expected_revision=rev+1).status_code==200
    assert reply(client,other,path,rev+2).status_code==200
    with client.session_factory.begin() as db:
        member = db.get(AIAppMember,(body.get('project_id',path.rsplit('/',1)[-1]),env_id))
        member.expires_at=now()-timedelta(seconds=1)
    assert not client.get(PREFIX+'/catalog',headers=other).json()['items']
    assert client.post(target+'/resolve',headers=other,json=run).status_code==403
    with client.session_factory() as db:
        assert db.get(AIAppEntry,entry['entry_id']) is not None
        assert db.scalar(select(AIAppRun).where(AIAppRun.conversation_id==run['conversation_id'])) is not None


def test_member_limit_adjustment_and_inflight_revocation_preserve_charges(versions, monkeypatch):
    client, claims, payload, _ = sponsored(versions, monkeypatch)
    with client.session_factory() as db:
        project = db.scalar(select(AIApplication)).project_id
    path=PREFIX+'/'+project
    # Use the original owner's trusted test BFF identity.
    auth=versions[1]
    other, env_id=guest(client,auth,versions[2])
    assert invite(client,auth,path,budget_micros=0).status_code==200
    assert reply(client,other,path).status_code==200
    _,_,_,runtime=launched(client,other,path,'b'*24)
    claims=jwt.decode(runtime['credential'][6:],get_settings().librechat_service_token,
        algorithms=['HS256'],audience='ai-application')
    with client.session_factory() as db:
        with pytest.raises(HTTPException,match='member_budget_exhausted'):
            admission(db,claims,payload,get_settings())
        assert not list(db.scalars(select(AIAppCall)))
    policy={'native_owner_id':OWNER,'native_id':'b'*24,'expected_revision':1,'budget_micros':50000}
    assert client.post(path+'/members/policy',headers=auth,json=policy).status_code==200
    assert client.post(path+'/members/policy',headers=auth,json=policy).status_code==409
    with client.session_factory() as db:
        identifier,engine,_,_=admission(db,claims,payload,get_settings())
    client.post(path+'/members/revoke',headers=auth,json={'native_owner_id':OWNER,'native_id':'b'*24})
    settle(engine,identifier,{'prompt_tokens':10,'completion_tokens':20},completed=True)
    with client.session_factory() as db:
        assert db.get(AIAppCall,identifier).held_micros==50
        with pytest.raises(HTTPException) as denied:
            admission(db,claims,{**payload,'messages':[{'role':'user','content':'next'}]},get_settings())
        assert denied.value.status_code==403
    current=client.get(path+'/releases',headers=auth).json()['members'][0]
    assert current['spent_micros']==50 and current['pending_micros']==0
    assert invite(client,auth,path,expected_revision=current['revision']).status_code==200
    assert reply(client,other,path,current['revision']+1).status_code==200
    policy.update(expected_revision=current['revision']+1,budget_micros=10)
    assert client.post(path+'/members/policy',headers=auth,json=policy).json()['members'][0]['held_micros']==50
    with client.session_factory() as db:
        with pytest.raises(HTTPException,match='member_budget_exhausted'):
            admission(db,claims,{**payload,'messages':[{'role':'user','content':'next'}]},get_settings())
