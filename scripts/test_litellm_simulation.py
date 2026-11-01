"""Run the real adapter against the local Postman simulation; test data only."""
import secrets
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from control_plane.litellm import LiteLLMError, LiteLLMGateway

run = secrets.token_hex(8)
with httpx.Client(base_url='http://127.0.0.1:34218/litellm', timeout=3, trust_env=False) as client:
    gateway = LiteLLMGateway(client, 'fake-master', ('simulation-model',))
    keys = []
    for subject in (run + '-a', run + '-b'):
        user = gateway.ensure_user('https://simulation.test', subject)
        candidate = 'sk-' + secrets.token_urlsafe(48)
        keys.append(gateway.ensure_key(user, candidate))
        assert gateway.ensure_user('https://simulation.test', subject) == user
        assert gateway.ensure_key(user, candidate, allow_create=False) == keys[-1]
    assert keys[0] != keys[1]
with httpx.Client(base_url='http://127.0.0.1:34219', timeout=1, trust_env=False) as client:
    try:
        LiteLLMGateway(client, 'fake-master', ('simulation-model',)).ensure_user('issuer', run)
    except LiteLLMError as error:
        assert str(error) == 'gateway_unavailable'
    else:
        raise AssertionError('Unreachable dependency must fail closed')
print('Postman simulation: two users, repeat provisioning and unavailable dependency passed')
