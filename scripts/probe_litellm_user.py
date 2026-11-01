"""Run inside LiteLLM pod; create and remove one isolated QA user/key, with no model call.

The API credential and generated virtual key are never printed. No real model
usage or payment is incurred. The generated user id is unique to this run.
"""
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

BASE = 'http://127.0.0.1:4000'
ADMIN = os.environ['LITELLM_MASTER_KEY']
user_id = 'codechat-qa-' + secrets.token_hex(12)
key = 'sk-' + secrets.token_urlsafe(36)
created_user = False
created_key = False


def request(path, body=None):
    headers = {'Authorization': 'Bearer ' + ADMIN}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=headers), timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, {}


try:
    status, user = request('/user/new', {'user_id': user_id, 'user_role': 'internal_user', 'max_budget': 1.0, 'auto_create_key': False, 'metadata': {'managed_by': 'codechat-qa'}})
    if status != 200:
        raise RuntimeError(f'user creation returned {status}')
    created_user = True
    if user.get('key'):
        raise RuntimeError('Unexpected automatic key')
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    missing_status, _ = request('/key/info?' + urllib.parse.urlencode({'key': key_hash}))
    print('Missing key lookup status:', missing_status)
    status, result = request('/key/generate', {'key': key, 'user_id': user_id, 'models': ['deepseek-v4-flash-v2'], 'max_budget': 1.0})
    if status != 200:
        raise RuntimeError(f'key creation returned {status}')
    created_key = True
    if result.get('key') != key:
        raise RuntimeError('Generated key does not match the requested key')
    status, key_info = request('/key/info?' + urllib.parse.urlencode({'key': key_hash}))
    if status != 200 or key_info.get('info', {}).get('user_id') != user_id or key_info.get('info', {}).get('models') != ['deepseek-v4-flash-v2']:
        raise RuntimeError('Key is not associated with the expected user')
    status, info = request('/user/info?' + urllib.parse.urlencode({'user_id': user_id}))
    if status != 200:
        raise RuntimeError(f'user lookup returned {status}')
    record = info.get('user_info', {})
    if record.get('user_id') != user_id or float(record.get('max_budget', -1)) != 1.0:
        raise RuntimeError('User identity or budget mismatch')
    if record.get('metadata', {}).get('managed_by') != 'codechat-qa':
        raise RuntimeError('Managed identity marker was not retained')
    print('User identity, one-dollar budget and supplied key creation verified')
finally:
    if created_key:
        status, _ = request('/key/delete', {'keys': [key]})
        if status != 200:
            raise RuntimeError(f'QA key cleanup returned {status}')
    if created_user:
        status, _ = request('/user/delete', {'user_ids': [user_id]})
        if status != 200:
            raise RuntimeError(f'QA user cleanup returned {status}')
    print('Temporary LiteLLM user and key cleaned up')
