import hashlib
import json

import httpx
import pytest

from control_plane.litellm import LiteLLMError, LiteLLMGateway, identity_hash, model_user_id


class FakeGateway:
    def __init__(self):
        self.users = {}
        self.keys = {}
        self.calls = []
        self.fail_after_key_write = False

    def __call__(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        if path == '/user/info':
            user = self.users.get(request.url.params['user_id'])
            return httpx.Response(200, json={'user_info': user}) if user else httpx.Response(404, json={})
        if path == '/user/new':
            body = json.loads(request.content)
            assert body['auto_create_key'] is False
            assert body['max_budget'] == 1.0
            assert 'budget_duration' not in body
            self.users[body['user_id']] = body
            return httpx.Response(200, json={'user_id': body['user_id']})
        if path == '/key/info':
            key = self.keys.get(request.url.params['key'])
            return httpx.Response(200, json={'info': key}) if key else httpx.Response(404, json={})
        if path == '/key/generate':
            body = json.loads(request.content)
            hashed = hashlib.sha256(body['key'].encode()).hexdigest()
            self.keys[hashed] = {'user_id': body['user_id'], 'models': body['models']}
            if self.fail_after_key_write:
                self.fail_after_key_write = False
                raise httpx.ReadTimeout('response lost', request=request)
            return httpx.Response(200, json={'key': body['key']})
        raise AssertionError(path)


@pytest.fixture
def gateway():
    fake = FakeGateway()
    with httpx.Client(base_url='http://litellm.test', transport=httpx.MockTransport(fake)) as client:
        yield LiteLLMGateway(client, 'never-log-this', ('model-a',)), fake


def test_user_grant_is_once_and_never_resets_existing_budget(gateway):
    api, fake = gateway
    user_id = api.ensure_user('issuer', 'alice')
    assert user_id == model_user_id('issuer', 'alice')
    fake.users[user_id]['max_budget'] = 0.25  # an administrator's later decision
    assert api.ensure_user('issuer', 'alice') == user_id
    assert fake.users[user_id]['max_budget'] == 0.25
    assert fake.calls.count(('POST', '/user/new')) == 1
    assert model_user_id('issuer', 'alice') != model_user_id('issuer', 'bob')
    assert identity_hash('ab', 'c') != identity_hash('a', 'bc')


def test_unmanaged_user_is_not_claimed_or_modified(gateway):
    api, fake = gateway
    identifier = model_user_id('issuer', 'alice')
    fake.users[identifier] = {'user_id': identifier, 'metadata': {'managed_by': 'someone-else'}}
    with pytest.raises(LiteLLMError, match='identity_conflict'):
        api.ensure_user('issuer', 'alice')
    assert ('POST', '/user/new') not in fake.calls


def test_key_is_reconciled_after_response_lost_without_duplicate(gateway):
    api, fake = gateway
    user_id = api.ensure_user('issuer', 'alice')
    candidate = 'sk-' + 'x' * 40
    fake.fail_after_key_write = True
    with pytest.raises(LiteLLMError, match='gateway_unavailable') as error:
        api.ensure_key(user_id, candidate)
    assert candidate not in str(error.value)
    assert api.ensure_key(user_id, candidate) == hashlib.sha256(candidate.encode()).hexdigest()
    assert fake.calls.count(('POST', '/key/generate')) == 1
    assert len(fake.keys) == 1


def test_key_owner_and_models_are_verified(gateway):
    api, fake = gateway
    candidate = 'sk-' + 'y' * 40
    hashed = hashlib.sha256(candidate.encode()).hexdigest()
    fake.keys[hashed] = {'user_id': 'someone-else', 'models': ['model-a']}
    with pytest.raises(LiteLLMError, match='identity_conflict'):
        api.ensure_key('expected-user', candidate)
    assert ('POST', '/key/generate') not in fake.calls
