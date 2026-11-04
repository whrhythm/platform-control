"""Administrator-selected real models and settlement policy, never provider keys."""
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError

from .database import get_db
from .models import AIConfiguration, AuditEvent
from .settings import get_settings


class AIPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chat_model: str = Field(min_length=1, max_length=200)
    coding_model: str = Field(min_length=1, max_length=200)
    allowed_models: list[str] = Field(min_length=1, max_length=30)
    cny_per_usd: str = Field(pattern=r'^\d{1,4}(?:\.\d{1,6})?$')
    plans: list[dict] = Field(min_length=1, max_length=10)

    @model_validator(mode='after')
    def validate_policy(self):
        from .capacity import CapacityPlan
        if (self.chat_model not in self.allowed_models or self.coding_model not in self.allowed_models or
            len(set(self.allowed_models)) != len(self.allowed_models) or
            any(not isinstance(value, str) or not 1 <= len(value) <= 200 or value in ('OpenHands', 'codechat-chat', 'codechat-coding') for value in self.allowed_models) or
            not Decimal('0.01') <= Decimal(self.cny_per_usd) <= Decimal('1000')):
            raise ValueError('Invalid model or settlement policy')
        self.plans = [CapacityPlan.model_validate(plan).model_dump() for plan in self.plans]
        if len({plan['id'] for plan in self.plans}) != len(self.plans):
            raise ValueError('Duplicate plans')
        return self


def effective_policy(db, settings):
    from .capacity import CapacityPlan
    row = db.get(AIConfiguration, 'default')
    if row:
        return {**row.config, 'plans': [CapacityPlan.model_validate(plan).model_dump() for plan in row.config['plans']]}
    import json
    allowed = [value.strip() for value in settings.litellm_models.split(',') if value.strip()]
    first = allowed[0] if allowed else ''
    return {'chat_model': settings.capability_chat_model or first, 'coding_model': first,
            'allowed_models': allowed, 'cny_per_usd': settings.model_settlement_cny_per_usd,
            'plans': [CapacityPlan.model_validate(plan).model_dump() for plan in json.loads(settings.capacity_plans_json)]}


def public_models(db, settings):
    policy = effective_policy(db, settings)
    row = db.get(AIConfiguration, 'default')
    catalog = {value['name']: value for value in row.catalog} if row else {}
    return {'chat_model': policy['chat_model'], 'coding_model': policy['coding_model'],
            'models': [catalog.get(name, {'name': name, 'input_cost_per_token': None,
                       'output_cost_per_token': None}) for name in policy['allowed_models']]}


def provider_catalog(settings):
    try:
        with httpx.Client(base_url=settings.litellm_url, headers={'Authorization': 'Bearer ' + settings.litellm_master_key},
                          timeout=10, trust_env=False) as client:
            response = client.get('/model/info')
            response.raise_for_status()
            values = response.json()['data']
        result = []
        for value in values:
            name, info = value.get('model_name'), value.get('model_info', {})
            if not isinstance(name, str) or not 1 <= len(name) <= 200 or name in ('OpenHands', 'codechat-chat', 'codechat-coding'):
                continue
            def price(key):
                value = info.get(key)
                return float(value) if isinstance(value, (int, float)) and 0 < value < 1 else None
            result.append({'name': name, 'input_cost_per_token': price('input_cost_per_token'),
                           'output_cost_per_token': price('output_cost_per_token')})
        return result
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(503, 'ai_catalog_unavailable') from None


def admin_router(admin_dependency):
    router = APIRouter(prefix='/v1/admin/ai', tags=['administration'])

    @router.get('')
    def get_policy(principal=Depends(admin_dependency), db=Depends(get_db)):
        row = db.get(AIConfiguration, 'default')
        return {'revision': row.revision if row else 0, 'policy': effective_policy(db, get_settings()),
                'catalog': provider_catalog(get_settings())}

    class SavePolicy(BaseModel):
        model_config = ConfigDict(extra='forbid')
        revision: int = Field(ge=0)
        policy: AIPolicy

    @router.put('')
    def save_policy(body: SavePolicy, principal=Depends(admin_dependency), db=Depends(get_db)):
        names = {value['name'] for value in provider_catalog(get_settings())}
        if not set(body.policy.allowed_models) <= names:
            raise HTTPException(409, 'ai_models_changed')
        row = db.get(AIConfiguration, 'default', with_for_update=True)
        if (row.revision if row else 0) != body.revision:
            raise HTTPException(409, 'ai_policy_changed')
        if row is None:
            row = AIConfiguration(id='default', revision=0)
            db.add(row)
        row.revision += 1
        row.config = body.policy.model_dump()
        db.add(AuditEvent(tenant_id='platform', issuer=principal.issuer, subject=principal.subject,
                          action='ai.policy.updated', target_id='default'))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, 'ai_policy_changed') from None
        return {'revision': row.revision, 'policy': row.config}

    return router
