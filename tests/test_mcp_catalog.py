import json
from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select
from control_plane.models import MCPServer, MCPCredential, UserEnvironment, AgentTask
from control_plane.mcp_catalog import endpoint, safe_schema, secret_for, decrypt
from control_plane.agent_mcp import execute
from control_plane.agent_execution import TaskTools, TaskStopped
from control_plane.agent_tasks import submit_task
from control_plane.auth import Principal
from control_plane.settings import get_settings
from test_agent_workbench import workspace


@pytest.fixture
def catalog(workspace, harness, monkeypatch):
    client, headers, base = workspace
    _, token = harness
    monkeypatch.setenv('PLATFORM_MCP_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    get_settings.cache_clear()
    admin = {'Authorization': 'Bearer ' + token('operator', groups=['platform-admins'])}
    tools = [{'name':'read_notes','description':'Read owned notes','inputSchema':{'type':'object','properties':{},'additionalProperties':False},'fingerprint':'read','supported':True},
             {'name':'create_note','description':'Create owned note','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False},'fingerprint':'write','supported':True}]
    async def gateway(*args, **kwargs):
        return {'tools':tools,'addresses':['8.8.8.8']}
    monkeypatch.setattr('control_plane.mcp_admin.gateway', gateway)
    return client, headers, base, admin


def create(catalog, auth='none'):
    client, _, _, admin = catalog
    body={'revision':0,'name':'Synthetic MCP','description':'Test only','url':'https://docs.example.com/mcp',
          'auth':auth,'data_notice':'Arguments leave the platform'}
    if auth=='platform_bearer':body['secret']='synthetic-shared-key'
    response=client.post('/v1/admin/mcp',headers=admin,json=body)
    assert response.status_code==200,response.text
    return response.json()


def publish(catalog, row, tools=None):
    client, _, _, admin=catalog
    path='/v1/admin/mcp/'+row['id']
    response=client.post(path+'/probe',headers=admin,json={'revision':row['revision'],**({'secret':'synthetic-probe-key'} if row['auth']=='user_bearer' else {})})
    assert response.status_code==200,response.text
    row=response.json()
    response=client.post(path+'/publish',headers=admin,json={'revision':row['revision'],'acknowledged':True,'tools':tools or {'read_notes':'read'}})
    assert response.status_code==200,response.text
    return response.json()


def test_admin_review_lifecycle_and_ordinary_user_cannot_publish(catalog,harness):
    client, headers, base, admin=catalog
    _,token=harness
    assert client.get('/v1/admin/mcp',headers={'Authorization':'Bearer '+token()}).status_code==403
    row=create(catalog)
    assert row['state']=='draft' and row['id'] not in [s['id'] for s in client.get(base+'/mcp',headers=headers).json()['servers']]
    path='/v1/admin/mcp/'+row['id']
    assert client.post(path+'/publish',headers=admin,json={'revision':row['revision'],'tools':{'read_notes':'read'},'acknowledged':True}).status_code==409
    row=publish(catalog,row)
    assert row['id'] in [s['id'] for s in client.get(base+'/mcp',headers=headers).json()['servers']]
    assert client.post(path+'/disable',headers=admin,json={'revision':1}).status_code==409
    response=client.post(path+'/disable',headers=admin,json={'revision':row['revision']})
    assert response.json()['state']=='disabled'
    assert next(s for s in client.get(base+'/mcp',headers=headers).json()['servers'] if s['id']==row['id'])['enabled'] is False


def test_owner_credentials_encrypted_not_returned_and_revocable_after_disable(catalog):
    client, headers, base, admin=catalog
    row=publish(catalog,create(catalog,'user_bearer'))
    path=base+'/mcp/'+row['id']+'/credential'
    pref=client.get(base+'/preferences',headers=headers).json()
    assert client.put(base+'/preferences',headers=headers,json={**pref,'mcp_servers':[row['id']]}).status_code==409
    for name,key in [('alice','synthetic-alice-key'),('bob','synthetic-bob-key')]:
        response=client.put(path,headers={**headers,'X-CodeChat-Subject':name},json={'revision':0,'secret':key})
        assert response.status_code==200 and key not in response.text
    with client.session_factory() as db:
        for env in db.scalars(select(UserEnvironment)):
            key,rev=secret_for(db,get_settings(),db.get(MCPServer,row['id']),env.id)
            assert key=='synthetic-'+env.subject+'-key' and rev==1
        records=list(db.scalars(select(MCPCredential)))
        assert all('synthetic-' not in record.encrypted_value for record in records)
        with pytest.raises(ValueError):
            decrypt(records[0].encrypted_value,get_settings(),records[1].environment_id,row['id'],1)
    assert 'synthetic-alice-key' not in client.get(base+'/mcp',headers=headers).text
    assert 'synthetic-alice-key' not in client.get('/v1/admin/mcp',headers=admin).text
    assert client.post('/v1/admin/mcp/'+row['id']+'/disable',headers=admin,json={'revision':row['revision']}).status_code==200
    assert client.put(path,headers=headers,json={'revision':1,'secret':None}).json()['configured'] is False


def test_platform_shared_credentials_cannot_publish_write_tools(catalog):
    client, _, _, admin=catalog
    row=create(catalog,'platform_bearer');path='/v1/admin/mcp/'+row['id']
    assert row['has_credential'] and 'synthetic-shared-key' not in json.dumps(row)
    row=client.post(path+'/probe',headers=admin,json={'revision':row['revision']}).json()
    assert client.post(path+'/publish',headers=admin,json={'revision':row['revision'],'acknowledged':True,'tools':{'create_note':'write'}}).status_code==422


def test_unknown_write_stops_without_replay_and_tools_not_approved_are_denied(catalog,monkeypatch):
    client,headers,base,_=catalog
    row=publish(catalog,create(catalog),{'create_note':'write'})
    pref=client.get(base+'/preferences',headers=headers).json()
    assert client.put(base+'/preferences',headers=headers,json={**pref,'mcp_servers':[row['id']]}).status_code==200
    with client.session_factory() as db:
        task=submit_task(db,Principal(get_settings().oidc_issuer,'alice'),get_settings(),'write-test','synthetic write','test-model')
    with client.session_factory.begin() as db:db.get(AgentTask,task.id).status='running'
    context=TaskTools(client.session_factory,task.id,None,get_settings(),None)
    assert 'unavailable' in execute(context,row['id'],'read_notes',{})
    calls=[]
    async def failed(*args,**kwargs):calls.append(kwargs);return {'error':'mcp_unavailable'}
    monkeypatch.setattr('control_plane.mcp_client.gateway',failed)
    proposed=[]
    monkeypatch.setattr(context,'authorize',lambda operation,arguments:proposed.append(operation))
    with pytest.raises(TaskStopped,match='write_requires_review'):execute(context,row['id'],'create_note',{'text':'test'})
    assert proposed==['mcp_write'] and len(calls)==1


@pytest.mark.parametrize('url',['http://docs.example.com/mcp','https://127.0.0.1/mcp','https://a:b@docs.example.com/mcp','https://docs.example.com:444/mcp','https://docs.example.com/mcp?token=x'])
def test_endpoints_refuse_unsafe_forms(url):
    with pytest.raises(ValueError):endpoint(url)


def test_schema_references_and_regex_are_not_executed():
    assert not safe_schema({'type':'object','$ref':'https://localhost/internal'})
    assert not safe_schema({'type':'object','properties':{'x':{'type':'string','pattern':'(a+)+$'}}})
