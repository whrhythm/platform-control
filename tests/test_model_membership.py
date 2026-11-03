from copy import deepcopy
import json

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import select

from control_plane.ai_policy import AIPolicy
from control_plane.litellm import LiteLLMError, identity_hash, model_user_id
from control_plane.model_billing_controller import apply_credit, sample_wallets
from control_plane.model_billing_provider import ModelBillingGateway, ModelBudgetConflict
from control_plane.models import CapacityAccount, CapacityOrder, ModelCredential, ModelCredit, ModelWallet, UserEnvironment
from control_plane.settings import get_settings
from test_capacity import capacity_harness, create_order, PREFIX


@pytest.fixture
def membership(capacity_harness, monkeypatch):
    client, auth, env_id, *_ = capacity_harness
    encryption = Fernet.generate_key()
    key = 'sk-' + 'synthetic-owned-key-' * 3
    monkeypatch.setenv('PLATFORM_LITELLM_MODELS', 'model')
    monkeypatch.setenv('PLATFORM_CAPABILITY_CHAT_MODEL', 'model')
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', encryption.decode())
    monkeypatch.setenv('PLATFORM_CAPACITY_PLANS_JSON', json.dumps([{'id':'basic','version':'test-v2','name':'基础版',
        'storage_gib':4,'application_limit':5,'amount_minor':1000,'currency':'CNY','model_credit_cny_minor':1000}]))
    get_settings.cache_clear()
    with client.session_factory.begin() as db:
        env = db.get(UserEnvironment, env_id)
        db.add(ModelCredential(issuer=env.issuer,subject=env.subject,status='ready',encrypted_key=Fernet(encryption).encrypt(key.encode()).decode()))
    state = {'user':{'user_id':model_user_id(env.issuer,env.subject),'max_budget':1.0,'spend':0.4,'models':['model'],
        'metadata':{'managed_by':'codechat','identity_hash':identity_hash(env.issuer,env.subject)}},
        'info':{'user_id':model_user_id(env.issuer,env.subject),'max_budget':1.0,'spend':0.4,'models':['model']},
        'writes':[], 'timeout':False}
    def request(req):
        path = req.url.path
        if req.method == 'GET' and path == '/user/info': return httpx.Response(200,json={'user_info':deepcopy(state['user'])})
        if req.method == 'GET' and path == '/key/info': return httpx.Response(200,json={'info':deepcopy(state['info'])})
        if req.method == 'GET' and path == '/user/daily/activity/aggregated':
            assert req.url.params['user_id'] == state['user']['user_id']
            return httpx.Response(200,json={'metadata':{'total_tokens':103,'total_prompt_tokens':100,'total_completion_tokens':3,'has_more':False}})
        if req.method == 'POST' and path in ('/user/update','/key/update'):
            body=json.loads(req.content);state['writes'].append((path,body))
            state['user' if path == '/user/update' else 'info'].update({k:v for k,v in body.items() if k in ('max_budget','models')})
            if state['timeout'] and path == '/user/update':
                state['timeout']=False
                raise httpx.ReadTimeout('synthetic committed write timeout')
            return httpx.Response(200,json={})
        raise AssertionError('Unexpected provider path')
    gateway = ModelBillingGateway(httpx.Client(base_url='http://provider.test',transport=httpx.MockTransport(request)), 'synthetic-master', ('model',))
    return client, auth, env, key, state, gateway


def test_credit_survives_committed_timeout_without_double_credit_or_spend_reset(membership):
    client, auth, env, key, state, gateway = membership
    created=create_order(client,auth).json(); identifier=created['id']
    assert created['plan']['model_credit_usd_micros'] == 1428571
    assert created['plan']['settlement_cny_per_usd'] == '7.000000'
    for _ in range(2):
        assert client.post(PREFIX+'/orders/'+identifier+'/confirm-test',headers=auth).status_code == 200
    apply_credit(client.session_factory,identifier,gateway,get_settings())
    apply_credit(client.session_factory,identifier,gateway,get_settings())
    state['timeout']=True
    with pytest.raises(LiteLLMError): apply_credit(client.session_factory,identifier,gateway,get_settings())
    assert state['user']['max_budget'] == 2.428571 and state['info']['max_budget'] == 1
    apply_credit(client.session_factory,identifier,gateway,get_settings())
    for _ in range(3): apply_credit(client.session_factory,identifier,gateway,get_settings())
    assert [path for path,_ in state['writes']] == ['/user/update','/key/update']
    assert state['user']['spend'] == state['info']['spend'] == 0.4
    assert state['user']['max_budget'] == state['info']['max_budget'] == 2.428571
    with client.session_factory() as db:
        row=db.get(ModelCredit,identifier)
        assert row.status == 'applied' and key not in json.dumps(row.evidence)
        assert db.get(CapacityOrder,identifier).evidence['model_credit_status'] == 'applied'
    sample_wallets(client.session_factory,get_settings(),gateway)
    wallet=client.get(PREFIX,headers=auth).json()['wallet']
    assert wallet['remaining_usd_micros'] == 2028571 and wallet['total_tokens'] == 103


def test_external_budget_changes_are_not_overwritten(membership):
    client,auth,env,key,state,gateway=membership
    evidence=gateway.targets(env,key,1000000)
    state['user']['max_budget']=5
    with pytest.raises(ModelBudgetConflict): gateway.apply(env,key,evidence)
    assert state['writes'] == [] and state['user']['max_budget'] == 5


def test_smaller_paid_plan_keeps_higher_file_and_application_rights(membership):
    client,auth,env,*_=membership
    client.get(PREFIX,headers=auth)
    with client.session_factory.begin() as db:
        current=db.get(UserEnvironment,env.id);current.storage_gib=8
        account=db.get(CapacityAccount,env.id)
        account.plan={**account.plan,'id':'pro','storage_gib':8,'application_limit':10}
    order=create_order(client,auth).json()
    assert order['plan']['storage_gib'] == 8 and order['plan']['application_limit'] == 10
    with client.session_factory() as db: assert db.get(CapacityOrder,order['id']).evidence['keep_current_plan'] is True


def test_admin_policy_requires_available_defaults_and_valid_settlement_rate():
    valid={'chat_model':'a','coding_model':'b','allowed_models':['a','b'],'cny_per_usd':'7.2',
        'plans':[{'id':'basic','version':'v1','name':'基础','storage_gib':4,'application_limit':5,'amount_minor':1000,'currency':'CNY'}]}
    assert AIPolicy.model_validate(valid).coding_model == 'b'
    for change in ({'cny_per_usd':'0'},{'allowed_models':['a']},{'allowed_models':['a','b','codechat-chat']}):
        with pytest.raises(ValueError): AIPolicy.model_validate({**valid,**change})


def test_admin_model_policy_permissions_catalog_and_conflicting_revision(harness, monkeypatch):
    client,token=harness
    monkeypatch.setattr('control_plane.ai_policy.provider_catalog',lambda s:[{'name':'a'},{'name':'b'}])
    ordinary={'Authorization':'Bearer '+token('alice')}
    admin={'Authorization':'Bearer '+token('operator',groups=['platform-admins'])}
    assert client.get('/v1/admin/ai',headers=ordinary).status_code == 403
    current=client.get('/v1/admin/ai',headers=admin)
    assert current.status_code == 200 and current.json()['revision'] == 0
    policy={'chat_model':'a','coding_model':'b','allowed_models':['a','b'],'cny_per_usd':'7',
        'plans':[{'id':'basic','version':'v1','name':'基础','storage_gib':4,'application_limit':5,'amount_minor':1000,'currency':'CNY','model_credit_cny_minor':1000}]}
    assert client.put('/v1/admin/ai',headers=ordinary,json={'revision':0,'policy':policy}).status_code == 403
    saved=client.put('/v1/admin/ai',headers=admin,json={'revision':0,'policy':policy})
    assert saved.status_code == 200 and saved.json()['revision'] == 1
    assert client.put('/v1/admin/ai',headers=admin,json={'revision':0,'policy':policy}).status_code == 409
    assert client.get('/v1/admin/ai',headers=admin).json()['policy']['coding_model'] == 'b'
