"""LiteLLM personal budgets with durable absolute targets, no spend resets."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib

from .litellm import LiteLLMError, LiteLLMGateway, identity_hash, model_user_id


class ModelBudgetConflict(LiteLLMError):
    pass


def micros(value):
    if isinstance(value, bool) or value is None:
        raise ModelBudgetConflict('model_budget_requires_review')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 1000000000:
            raise ValueError()
        return int((amount * 1000000).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        raise ModelBudgetConflict('model_budget_requires_review') from None


class ModelBillingGateway(LiteLLMGateway):
    def account(self, env, key):
        user_id, fingerprint = model_user_id(env.issuer, env.subject), hashlib.sha256(key.encode()).hexdigest()
        user = self._lookup_user(user_id)
        response = self._request('GET', '/key/info', params={'key': fingerprint})
        info = self._json(response).get('info') if response.status_code == 200 else None
        if (not user or not isinstance(user.get('metadata'), dict) or user.get('metadata', {}).get('managed_by') != 'codechat' or
            user.get('metadata', {}).get('identity_hash') != identity_hash(env.issuer, env.subject) or
            user.get('user_id') != user_id or not isinstance(info, dict) or info.get('user_id') != user_id or
            info.get('blocked') is True or info.get('team_id') or user.get('budget_duration') or info.get('budget_duration')):
            raise ModelBudgetConflict('model_identity_requires_review')
        return user, info

    def targets(self, env, key, amount):
        user, info = self.account(env, key)
        before_user, before_key = micros(user.get('max_budget')), micros(info.get('max_budget'))
        return {'user_id': user['user_id'], 'key_hash': hashlib.sha256(key.encode()).hexdigest(),
                'before_user': before_user, 'before_key': before_key,
                'target_user': before_user + amount, 'target_key': before_key + amount}

    def apply(self, env, key, evidence):
        user, info = self.account(env, key)
        if evidence['user_id'] != user['user_id'] or evidence['key_hash'] != hashlib.sha256(key.encode()).hexdigest():
            raise ModelBudgetConflict('model_identity_requires_review')
        for current, original, target in ((micros(user.get('max_budget')), evidence['before_user'], evidence['target_user']),
                                          (micros(info.get('max_budget')), evidence['before_key'], evidence['target_key'])):
            if current not in (original, target):
                raise ModelBudgetConflict('model_budget_changed_requires_review')
        for path, payload, current, target in (
            ('/user/update', {'user_id': user['user_id']}, micros(user['max_budget']), evidence['target_user']),
            ('/key/update', {'key': key}, micros(info['max_budget']), evidence['target_key'])):
            if current != target:
                response = self._request('POST', path, json={**payload, 'max_budget': target / 1000000})
                if response.status_code != 200:
                    raise LiteLLMError('model_credit_write_unconfirmed')
        user, info = self.account(env, key)
        if micros(user['max_budget']) != evidence['target_user'] or micros(info['max_budget']) != evidence['target_key']:
            raise LiteLLMError('model_credit_not_observed')

    def synchronize_models(self, env, key, allowed):
        user, info = self.account(env, key)
        for path, identity, current in (('/user/update', {'user_id': user['user_id']}, user),
                                         ('/key/update', {'key': key}, info)):
            names = current.get('models')
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise ModelBudgetConflict('model_policy_requires_review')
            if not set(allowed) <= set(names):
                response = self._request('POST', path, json={**identity, 'models': sorted(set(names) | set(allowed))})
                if response.status_code != 200:
                    raise LiteLLMError('model_policy_not_applied')

    def sample(self, env, key):
        user, info = self.account(env, key)
        user_limit, key_limit = micros(user.get('max_budget')), micros(info.get('max_budget'))
        user_spend, key_spend = micros(user.get('spend', 0)), micros(info.get('spend', 0))
        start, end = env.created_at.date().isoformat(), datetime.now(timezone.utc).date().isoformat()
        response = self._request('GET', '/user/daily/activity/aggregated', params={
            'user_id': user['user_id'], 'start_date': start, 'end_date': end, 'include_current_utc_day': 'true'})
        metrics = self._json(response).get('metadata', {}) if response.status_code == 200 else {}
        def count(name):
            value = metrics.get(name)
            return value if type(value) is int and 0 <= value < 2 ** 63 and metrics.get('has_more') is not True else None
        return {'allocated_usd_micros': min(user_limit, key_limit), 'spent_usd_micros': max(user_spend, key_spend),
                'remaining_usd_micros': max(0, min(user_limit - user_spend, key_limit - key_spend)),
                'total_tokens': count('total_tokens'), 'prompt_tokens': count('total_prompt_tokens'),
                'completion_tokens': count('total_completion_tokens'), 'usage_start': start, 'usage_end': end}
