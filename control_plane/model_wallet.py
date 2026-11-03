"""Observed model money and tokens; estimates are never spending limits."""
from datetime import timezone
from decimal import Decimal
from datetime import datetime
from typing import Literal
from pydantic import BaseModel

from .ai_policy import effective_policy
from .models import AIConfiguration, ModelWallet, now


class WalletOut(BaseModel):
    status: Literal['ok', 'unknown', 'stale']
    allocated_usd_micros: int | None = None
    spent_usd_micros: int | None = None
    remaining_usd_micros: int | None = None
    remaining_cny_minor: int | None = None
    cny_per_usd: str | None = None
    total_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_tokens: int | None = None
    reference_model: str | None = None
    usage_start: str | None = None
    usage_end: str | None = None
    observed_at: datetime | None = None


def estimate_tokens(db, settings, amount_micros):
    name = effective_policy(db, settings)['chat_model']
    row = db.get(AIConfiguration, 'default')
    model = next((value for value in row.catalog if value['name'] == name), None) if row else None
    tokens = None
    if model and model.get('input_cost_per_token') and model.get('output_cost_per_token'):
        average = (Decimal(str(model['input_cost_per_token'])) + Decimal(str(model['output_cost_per_token']))) / 2
        tokens = int(Decimal(amount_micros) / 1000000 / average)
    return {'estimated_tokens': tokens, 'reference_model': name}


def wallet_out(db, env, settings):
    row = db.get(ModelWallet, env.id)
    if not row:
        return {'status': 'unknown', 'total_tokens': None, 'remaining_usd_micros': None}
    status = row.status
    if row.observed_at and (now() - row.observed_at.replace(tzinfo=timezone.utc)).total_seconds() > 180:
        status = 'stale'
    result = {**row.sample, 'status': status, 'observed_at': row.observed_at}
    rate = Decimal(effective_policy(db, settings)['cny_per_usd'])
    remaining = result.get('remaining_usd_micros')
    if remaining is not None:
        result.update(estimate_tokens(db, settings, remaining))
        result.update(cny_per_usd=str(rate), remaining_cny_minor=int(Decimal(remaining) * rate / 10000))
    return result
