"""LiteLLM provisioning adapter. No HTTP routes or credential persistence.

Callers MUST durably encrypt and save the candidate key before requesting its
creation, so a timeout can be reconciled without issuing a second key.
"""
import hashlib
import json

import httpx


class LiteLLMError(Exception):
    """Safe, stable error: never include provider response bodies or credentials."""


def identity_hash(issuer: str, subject: str) -> str:
    canonical = json.dumps([issuer, subject], ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()


def model_user_id(issuer: str, subject: str) -> str:
    return 'codechat-' + identity_hash(issuer, subject)


class LiteLLMGateway:
    def __init__(self, client: httpx.Client, master_key: str, models: tuple[str, ...]):
        if not master_key or not models:
            raise ValueError('LiteLLM credentials and allowed models are required')
        self.client = client
        self._headers = {'Authorization': 'Bearer ' + master_key}
        self.models = models

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return self.client.request(method, path, headers=self._headers, **kwargs)
        except httpx.RequestError:
            raise LiteLLMError('gateway_unavailable') from None

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            value = response.json()
        except ValueError:
            raise LiteLLMError('gateway_invalid_response') from None
        if not isinstance(value, dict):
            raise LiteLLMError('gateway_invalid_response')
        return value

    def _lookup_user(self, user_id: str) -> dict | None:
        response = self._request('GET', '/user/info', params={'user_id': user_id})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise LiteLLMError('gateway_user_lookup_failed')
        result = self._json(response).get('user_info')
        if not isinstance(result, dict):
            raise LiteLLMError('gateway_invalid_response')
        return result

    def ensure_user(self, issuer: str, subject: str, *, allow_create: bool = True) -> str:
        if not issuer or not subject:
            raise ValueError('Verified issuer and subject are required')
        digest = identity_hash(issuer, subject)
        user_id = model_user_id(issuer, subject)
        current = self._lookup_user(user_id)
        if current is None:
            if not allow_create:
                raise LiteLLMError('gateway_user_revoked')
            response = self._request('POST', '/user/new', json={
                'user_id': user_id,
                'user_role': 'internal_user',
                'max_budget': 1.0,
                'models': list(self.models),
                'auto_create_key': False,
                'metadata': {'managed_by': 'codechat', 'identity_hash': digest},
            })
            # A conflicting create can occur on retry. Verify rather than update.
            current = self._lookup_user(user_id)
            if current is None:
                raise LiteLLMError('gateway_user_create_failed')
        metadata = current.get('metadata') if isinstance(current, dict) else None
        if not isinstance(metadata, dict) or metadata.get('managed_by') != 'codechat' or metadata.get('identity_hash') != digest or current.get('user_id') != user_id:
            raise LiteLLMError('gateway_user_identity_conflict')
        # Never set the budget again here: this would overwrite usage/administrator decisions.
        return user_id

    def ensure_key(self, user_id: str, candidate: str, *, allow_create: bool = True, require_models: bool = True) -> str:
        if not candidate.startswith('sk-') or len(candidate) < 35:
            raise ValueError('A securely generated, previously persisted key is required')
        key_hash = hashlib.sha256(candidate.encode()).hexdigest()
        response = self._request('GET', '/key/info', params={'key': key_hash})
        if response.status_code == 404:
            if not allow_create:
                raise LiteLLMError('gateway_key_revoked')
            response = self._request('POST', '/key/generate', json={
                'key': candidate, 'user_id': user_id, 'models': list(self.models), 'max_budget': 1.0,
            })
            response = self._request('GET', '/key/info', params={'key': key_hash})
        if response.status_code != 200:
            raise LiteLLMError('gateway_key_lookup_failed')
        info = self._json(response).get('info')
        if not isinstance(info, dict) or info.get('user_id') != user_id or (require_models and not set(self.models) <= set(info.get('models') or [])):
            raise LiteLLMError('gateway_key_identity_conflict')
        return key_hash
