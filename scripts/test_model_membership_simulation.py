"""Actual HTTP credit adapter against a local Postman simulation; synthetic keys."""
from datetime import datetime, timezone
import os
from types import SimpleNamespace

import httpx

from control_plane.litellm import LiteLLMError
from control_plane.model_billing_provider import ModelBillingGateway

base=os.environ['MEMBERSHIP_SIM_URL'].rstrip('/')+'/litellm/'
assert base.startswith('http://127.0.0.1:')
with httpx.Client(base_url=base,timeout=0.4,trust_env=False) as client:
    client.post('__test__/reset').raise_for_status()
    gateway=ModelBillingGateway(client,'synthetic-master',('sim-model',))
    env=SimpleNamespace(issuer='https://identity.synthetic',subject='alice',created_at=datetime.now(timezone.utc))
    user=gateway.ensure_user(env.issuer,env.subject)
    key='sk-'+'synthetic-test-key-'*3
    gateway.ensure_key(user,key)
    target=gateway.targets(env,key,1428571)
    client.post('__test__/fault',json={'after_user_update_ms':1500}).raise_for_status()
    try:gateway.apply(env,key,target)
    except LiteLLMError:pass
    else:raise AssertionError('The committed timeout did not reach the HTTP client')
    gateway.apply(env,key,target)
    gateway.apply(env,key,target)
    snapshot=client.get('__test__/snapshot').json()
    assert [v['path'] for v in snapshot['writes']] == ['/user/update','/key/update']
    sample=gateway.sample(env,key)
    assert sample['allocated_usd_micros']==2428571 and sample['spent_usd_micros']==400000
    assert sample['remaining_usd_micros']==2028571 and sample['total_tokens']==300
print('PASS: committed HTTP timeout recovers once, both limits increase, spend persists, actual token totals are attributed')
