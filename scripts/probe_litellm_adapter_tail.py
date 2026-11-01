"""Appended to control_plane/litellm.py and run inside the LiteLLM Pod."""
import os
import secrets

subject = 'integration-qa-' + secrets.token_hex(12)
issuer = 'https://integration-qa.invalid/'
candidate = 'sk-' + secrets.token_urlsafe(36)
user_id = model_user_id(issuer, subject)
headers = {'Authorization': 'Bearer ' + os.environ['LITELLM_MASTER_KEY']}
with httpx.Client(base_url='http://127.0.0.1:4000', timeout=30) as client:
    gateway = LiteLLMGateway(client, os.environ['LITELLM_MASTER_KEY'], ('deepseek-v4-flash-v2',))
    try:
        assert gateway.ensure_user(issuer, subject) == user_id
        assert gateway.ensure_user(issuer, subject) == user_id
        assert gateway.ensure_key(user_id, candidate) == hashlib.sha256(candidate.encode()).hexdigest()
        assert gateway.ensure_key(user_id, candidate) == hashlib.sha256(candidate.encode()).hexdigest()
        print('Live adapter: stable user, one-dollar budget and idempotent key verified')
    finally:
        key_result = client.post('/key/delete', headers=headers, json={'keys': [hashlib.sha256(candidate.encode()).hexdigest()]})
        user_result = client.post('/user/delete', headers=headers, json={'user_ids': [user_id]})
        if key_result.status_code not in (200, 404) or user_result.status_code not in (200, 404):
            raise RuntimeError('QA resource cleanup not confirmed')
        print('Live adapter QA resources cleaned up')
