"""Identity-bound test orders and observed, source-aware capacity accounts."""
import json
import hashlib
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select

from .database import get_db
from .models import (AuditEvent, CapacityAccount, CapacityOrder, StorageMeasurement,
                     UserApplication, WorkspaceFileService, WorkspaceVolumeBinding, ProjectDatabase, now)
from .models import UserObjectStorage
from .object_storage import ObjectStorageOut, storage_out
from .settings import get_settings
from .model_wallet import WalletOut, wallet_out, estimate_tokens
from .application_resources import CPU, MEMORY, enriched_plan, plan_resources
from .workspace_gateway import own_environment, call_files
from .code_execution import code_rights, usage as code_usage
from .plan_quotas import PlanQuotas, quota_rights, active_quotas, FIELDS as QUOTA_FIELDS

GIB = 1024 ** 3
COMPONENTS = ('files', 'database', 'objects', 'images', 'app_volumes')


class CapacityPlan(PlanQuotas):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[a-z][a-z0-9-]{0,39}$')
    version: str = Field(min_length=1, max_length=40, pattern=r'^[a-zA-Z0-9._-]+$')
    name: str = Field(min_length=1, max_length=60)
    storage_gib: int = Field(ge=1, le=40)
    application_limit: int = Field(ge=1, le=100)
    amount_minor: int = Field(ge=0, le=1000000)
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    model_credit_cny_minor: int = Field(default=0, ge=0, le=1000000)
    description: str = Field(default='', max_length=300)
    features: list[str] = Field(default_factory=list, max_length=12)
    application_cpu_millicores: int | None = Field(default=None, ge=50, le=4000)
    application_memory_mib: int | None = Field(default=None, ge=128, le=8192)
    code_runs_monthly: int | None = Field(default=None, ge=0, le=10000)
    code_timeout_seconds: int | None = Field(default=None, ge=1, le=15)
    code_concurrency: int | None = Field(default=None, ge=1, le=2)

    @model_validator(mode='after')
    def runtime_defaults(self):
        rights = plan_resources(self.model_dump())
        self.application_cpu_millicores, self.application_memory_mib = rights[CPU], rights[MEMORY]
        for key, value in code_rights(self.model_dump()).items():
            setattr(self, key, value)
        for key, value in quota_rights(self.model_dump()).items():
            setattr(self, key, value)
        if (self.applications_cpu_millicores < self.application_cpu_millicores or
                self.applications_memory_mib < self.application_memory_mib or
                self.database_per_instance_mib > self.database_budget_mib or
                self.database_budget_mib > self.storage_gib * 1024 or
                (self.database_limit > 0 and self.database_per_instance_mib == 0) or
                self.agent_command_seconds > self.agent_task_seconds or
                self.build_memory_mib < get_settings().build_scratch_mib + 512):
            raise ValueError('Inconsistent quota policy')
        return self


class CreateCapacityOrder(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_id: str = Field(min_length=1, max_length=40)
    plan_version: str = Field(min_length=1, max_length=40)


class CapacityPlanOut(PlanQuotas):
    id: str
    version: str
    catalog_version: str | None = None
    name: str
    storage_gib: int
    application_limit: int
    application_cpu_millicores: int
    application_memory_mib: int
    code_runs_monthly: int = 30
    code_timeout_seconds: int = 10
    code_concurrency: int = 1
    amount_minor: int
    currency: str
    model_credit_cny_minor: int = 0
    model_credit_usd_micros: int = 0
    settlement_cny_per_usd: str | None = None
    description: str = ''
    features: list[str] = Field(default_factory=list)
    estimated_tokens: int | None = None
    reference_model: str | None = None


class CapacityOrderOut(BaseModel):
    id: str
    plan: CapacityPlanOut
    status: Literal['pending_payment', 'applying', 'ready', 'failed', 'cancelled']
    payment_status: Literal['unpaid', 'confirmed']
    stage: str
    error_code: str | None
    attempt: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    model_credit_status: str = 'not_required'


class StorageComponentOut(BaseModel):
    component: Literal['files', 'database', 'objects', 'images', 'app_volumes', 'versions']
    budget_bytes: int | None
    used_bytes: int | None
    observed_at: datetime | None
    status: Literal['ok', 'stale', 'unknown', 'not_metered']
    basis: str | None = None
    scope_id: str | None = None


class StorageAccountOut(BaseModel):
    budget_bytes: int
    pending_capacity_bytes: int
    known_used_bytes: int | None
    complete: bool
    budget_complete: bool = False
    scope: Literal['managed_logical_storage'] = 'managed_logical_storage'
    components: list[StorageComponentOut]


class ApplicationAllowanceOut(BaseModel):
    used: int
    limit: int


class CapacityAccountOut(BaseModel):
    plan: CapacityPlanOut
    mode: Literal['test', 'disabled']
    payment_provider: Literal['manual_test', 'xunhupay_local'] = 'manual_test'
    plans: list[CapacityPlanOut]
    active_order: CapacityOrderOut | None
    orders: list[CapacityOrderOut]
    applications: ApplicationAllowanceOut
    storage: StorageAccountOut
    object_storage: ObjectStorageOut | None = None
    wallet: WalletOut | None = None
    trial: dict | None = None
    code_execution: dict | None = None
    quota_usage: dict | None = None


def catalog(settings, db=None):
    try:
        from decimal import Decimal, ROUND_DOWN
        from .ai_policy import effective_policy
        policy = effective_policy(db, settings) if db is not None else None
        raw = policy['plans'] if policy else json.loads(settings.capacity_plans_json)
        rate = Decimal(policy['cny_per_usd'] if policy else settings.model_settlement_cny_per_usd)
        if not Decimal('0.01') <= rate <= Decimal('1000'):
            raise ValueError()
        if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
            raise ValueError()
        plans = [CapacityPlan.model_validate(plan).model_dump() for plan in raw]
        if len({plan['id'] for plan in plans}) != len(plans):
            raise ValueError()
        for plan in plans:
            if plan['model_credit_cny_minor']:
                if plan['currency'] != 'CNY':
                    raise ValueError()
                plan['settlement_cny_per_usd'] = str(rate)
                plan['model_credit_usd_micros'] = int((Decimal(plan['model_credit_cny_minor']) * 10000 / rate).to_integral_value(rounding=ROUND_DOWN))
                if db is not None:
                    from .model_wallet import estimate_tokens
                    plan.update(estimate_tokens(db, settings, plan['model_credit_usd_micros']))
            # Every price/rights edit changes the offered version, even if an
            # administrator forgets to change their human catalog label.
            version = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]
            plan['catalog_version'], plan['version'] = plan['version'], version
        return plans
    except (ValueError, TypeError, ValidationError, ArithmeticError):
        raise HTTPException(503, 'capacity_configuration_invalid') from None


def account_for(db, env, settings):
    """Caller holds the environment lock before inserting or mutating its account."""
    account = db.get(CapacityAccount, env.id)
    if account is None:
        account = CapacityAccount(environment_id=env.id, plan={
            'id': 'existing', 'version': 'existing-v1', 'name': 'Existing allowance',
            'storage_gib': env.storage_gib, 'application_limit': settings.default_application_limit,
            'amount_minor': 0, 'currency': 'CNY'})
        db.add(account)
        db.flush()
    return account


def application_limit(db, env, settings):
    account = db.get(CapacityAccount, env.id)
    return account.plan['application_limit'] if account else settings.default_application_limit


def eligible(plan, account, env):
    if plan.get('model_credit_usd_micros', 0) > 0:
        return True
    return (plan['storage_gib'] >= env.storage_gib and
            plan['application_limit'] >= account.plan['application_limit'] and
            (plan['storage_gib'] > env.storage_gib or plan['application_limit'] > account.plan['application_limit'] or
             plan.get('model_credit_usd_micros', 0) > 0 or
             any(plan_resources(plan)[key] > plan_resources(account.plan)[key] for key in (CPU, MEMORY)) or
             any(code_rights(plan)[key] > code_rights(account.plan)[key] for key in code_rights(plan)) or
             any(quota_rights(plan)[key] > quota_rights(account.plan)[key] for key in QUOTA_FIELDS)))


def order_out(order):
    return {**{key: getattr(order, key) for key in ('id', 'plan', 'status', 'payment_status', 'stage',
        'error_code', 'attempt', 'created_at', 'started_at', 'completed_at')},
        'plan': enriched_plan(order.plan),
        'model_credit_status': order.evidence.get('model_credit_status', 'pending' if order.plan.get('model_credit_usd_micros', 0) else 'not_required')}


def sample_storage(db, env, observed):
    rows = {row.component: row for row in db.scalars(select(StorageMeasurement).where(
        StorageMeasurement.environment_id == env.id))}
    for component in COMPONENTS:
        if component not in rows:
            rows[component] = StorageMeasurement(environment_id=env.id, component=component,
                status='unknown' if component == 'files' or get_settings().storage_metering_enabled else 'not_metered')
            db.add(rows[component])
    row = rows['files']
    valid = False
    try:
        used = observed['used_bytes']
        at = datetime.fromisoformat(observed['observed_at'])
        valid = (type(used) is int and 0 <= used < 2 ** 63 and at.tzinfo is not None and
                 -60 <= (now() - at).total_seconds() <= 90 and observed['scope'] == 'personal_volume')
    except (TypeError, ValueError, KeyError):
        pass
    if valid:
        row.used_bytes, row.observed_at, row.status = used, at, 'ok'
        row.budget_bytes, row.basis, row.scope_id = env.storage_gib * GIB, 'personal_files_and_caches', env.id
    else:
        row.status = 'stale' if row.observed_at is not None else 'unknown'
    if get_settings().project_databases_enabled:
        databases = list(db.scalars(select(ProjectDatabase).where(ProjectDatabase.environment_id == env.id,
                                                                  ProjectDatabase.status != 'deleted')))
        measured = rows['database']
        if all(value.status == 'ready' and value.observed_at and value.used_bytes is not None and
               0 <= (now() - value.observed_at.replace(tzinfo=timezone.utc)).total_seconds() <= 90 for value in databases):
            measured.used_bytes = sum(value.used_bytes for value in databases)
            measured.observed_at = min((value.observed_at for value in databases), default=now())
            measured.status = 'ok'
        else:
            measured.status = 'stale' if measured.observed_at else 'unknown'
        measured.budget_bytes = active_quotas(db, env.id)['database_budget_mib'] * 1024 ** 2
        measured.basis, measured.scope_id = 'pg_database_size', env.id
    for component in COMPONENTS:
        row = rows[component]
        if row.observed_at and row.status == 'ok' and (now() - row.observed_at.replace(tzinfo=timezone.utc)).total_seconds() > get_settings().storage_sample_max_age_seconds:
            row.status = 'stale'
    return rows


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/capacity', tags=['capacity'])

    def enabled(settings):
        if not settings.capacity_upgrades_enabled or not settings.test_payments_enabled:
            raise HTTPException(403, 'capacity_test_payments_disabled')

    def locked_env(db, principal):
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        if env.status == 'suspended':
            raise HTTPException(403, 'capacity_account_suspended')
        return env

    def audit(db, principal, env, action, target):
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                          action='capacity.' + action, target_id=target))

    @router.get('', response_model=CapacityAccountOut)
    def get_account(principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = get_settings()
        env = own_environment(db, principal)
        try:
            observed = call_files(db, principal, settings, 'GET', '/v1/usage', '').json()
        except (HTTPException, ValueError):
            observed = None
        db.refresh(env, with_for_update=True)
        account = account_for(db, env, settings)
        samples = sample_storage(db, env, observed)
        orders = list(db.scalars(select(CapacityOrder).where(CapacityOrder.environment_id == env.id)
                                 .order_by(CapacityOrder.created_at.desc()).limit(10)))
        active = db.get(CapacityOrder, account.active_order_id) if account.active_order_id else None
        database_budget = active_quotas(db, env.id)['database_budget_mib'] * 1024 ** 2 if settings.project_databases_enabled else 0
        components = [{'component': kind, 'budget_bytes': (env.storage_gib * GIB if kind == 'files' else
                database_budget if kind == 'database' and database_budget else samples[kind].budget_bytes),
            'used_bytes': samples[kind].used_bytes, 'observed_at': samples[kind].observed_at,
            'status': samples[kind].status, 'basis': samples[kind].basis, 'scope_id': samples[kind].scope_id} for kind in COMPONENTS]
        fresh = [row for row in samples.values() if row.status == 'ok' and row.used_bytes is not None]
        complete = len(fresh) == len(COMPONENTS)
        version_bytes = 0
        if settings.project_versions_enabled:
            from .project_versions import version_usage
            version_bytes = version_usage(db, env.id)
            components.append({'component': 'versions', 'budget_bytes': None, 'used_bytes': version_bytes,
                'observed_at': now(), 'status': 'ok', 'basis': 'encrypted_source_bytes', 'scope_id': env.id})
        apps = db.scalar(select(func.count(UserApplication.id)).where(UserApplication.environment_id == env.id,
                                                                     UserApplication.status != 'deleted'))
        available = settings.capacity_upgrades_enabled and settings.test_payments_enabled
        from .plan_quotas import application_allocations
        from .models import AgentTask
        allocated = application_allocations(db, env.id)
        database_rows = list(db.scalars(select(ProjectDatabase).where(ProjectDatabase.environment_id == env.id,
            ProjectDatabase.status != 'deleted')))
        from .ai_policy import trial_plan
        result = {'plan': enriched_plan(account.plan), 'mode': 'test' if available else 'disabled',
            'payment_provider': 'xunhupay_local' if settings.xunhupay_simulator_enabled else 'manual_test',
            'quota_usage': {'application_cpu_millicores': allocated[CPU], 'application_memory_mib': allocated[MEMORY],
                'database_count': len(database_rows), 'database_reserved_bytes': sum(row.reserved_bytes for row in database_rows),
                'agent_active': db.scalar(select(func.count(AgentTask.id)).where(AgentTask.environment_id == env.id,
                    AgentTask.status.in_(('queued', 'running', 'awaiting_approval', 'cancelling')))),
                'build_active': db.scalar(select(func.count(UserApplication.id)).where(UserApplication.environment_id == env.id,
                    UserApplication.status.in_(('queued', 'preparing', 'building', 'deploying')))),
                'database_enforcement': 'allocation_and_warning', 'storage_enforcement': 'provider_limits_and_metering',
                'effective_at': account.updated_at},
            'code_execution': code_usage(db, env),
            'trial': {**trial_plan(db, settings),
                      **estimate_tokens(db, settings, 1000000)},
            'plans': [plan for plan in catalog(settings, db) if eligible(plan, account, env)] if available else [],
            'active_order': order_out(active) if active else None, 'orders': [order_out(order) for order in orders],
            'applications': {'used': apps, 'limit': account.plan['application_limit']},
            'object_storage': storage_out(db.get(UserObjectStorage, env.id), settings) if settings.object_storage_enabled else None,
            'wallet': wallet_out(db, env, settings),
            # Service-specific caps are sub-limits, not additional purchased storage.
            'storage': {'budget_bytes': account.plan['storage_gib'] * GIB,
                'pending_capacity_bytes': max(0, (active.plan['storage_gib'] - env.storage_gib) * GIB) if active else 0,
                'known_used_bytes': sum(row.used_bytes for row in fresh) + version_bytes if fresh or settings.project_versions_enabled else None,
                'complete': complete, 'budget_complete': True,
                'components': components}}
        db.commit()
        return result

    @router.post('/orders', status_code=201, response_model=CapacityOrderOut)
    def create_order(body: CreateCapacityOrder, principal=Depends(principal_dependency), db=Depends(get_db),
                     idempotency_key: str = Header(min_length=16, max_length=128, pattern=r'^[a-zA-Z0-9_-]+$')):
        settings = get_settings()
        enabled(settings)
        env = locked_env(db, principal)
        account = account_for(db, env, settings)
        previous = db.scalar(select(CapacityOrder).where(CapacityOrder.environment_id == env.id,
                                                        CapacityOrder.idempotency_key == idempotency_key))
        if previous:
            if previous.plan['id'] != body.plan_id or previous.plan['version'] != body.plan_version:
                raise HTTPException(409, 'capacity_idempotency_conflict')
            return order_out(previous)
        if account.active_order_id:
            raise HTTPException(409, 'capacity_order_in_progress')
        plan = next((plan for plan in catalog(settings, db) if plan['id'] == body.plan_id), None)
        if not plan or plan['version'] != body.plan_version:
            raise HTTPException(409, 'capacity_plan_changed')
        if not eligible(plan, account, env):
            raise HTTPException(409, 'capacity_downgrade_unavailable')
        service = db.get(WorkspaceFileService, env.id)
        if not service or service.status != 'ready' or not db.get(WorkspaceVolumeBinding, env.id):
            raise HTTPException(409, 'capacity_space_preparing')
        keep = (plan['storage_gib'] < env.storage_gib or plan['application_limit'] < account.plan['application_limit'] or
                any(plan_resources(plan)[key] < plan_resources(account.plan)[key] for key in (CPU, MEMORY)))
        snapshot = {**plan, 'storage_gib': max(plan['storage_gib'], env.storage_gib),
                    'application_limit': max(plan['application_limit'], account.plan['application_limit']),
                    **{key: max(plan_resources(plan)[key], plan_resources(account.plan)[key]) for key in (CPU, MEMORY)},
                    **{key: max(code_rights(plan)[key], code_rights(account.plan)[key]) for key in code_rights(plan)},
                    **{key: max(quota_rights(plan)[key], quota_rights(account.plan)[key]) for key in QUOTA_FIELDS}}
        order = CapacityOrder(environment_id=env.id, idempotency_key=idempotency_key, plan=snapshot,
                              evidence={'keep_current_plan': keep})
        db.add(order)
        db.flush()
        account.active_order_id = order.id
        audit(db, principal, env, 'order_created', order.id)
        db.commit()
        return order_out(order)

    @router.post('/orders/{order_id}/{action}', response_model=CapacityOrderOut)
    def order_action(order_id: str, action: str, principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = get_settings()
        enabled(settings)
        env = locked_env(db, principal)
        account = account_for(db, env, settings)
        order = db.scalar(select(CapacityOrder).where(CapacityOrder.id == order_id,
                            CapacityOrder.environment_id == env.id).with_for_update())
        if not order:
            raise HTTPException(404, 'capacity_order_not_found')
        if action not in ('confirm-test', 'retry', 'cancel'):
            raise HTTPException(404, 'capacity_action_not_found')
        if action == 'confirm-test' and settings.xunhupay_simulator_enabled:
            raise HTTPException(409, 'payment_checkout_required')
        if action == 'confirm-test' and order.payment_status == 'confirmed':
            return order_out(order)
        if action == 'cancel' and order.status == 'cancelled':
            return order_out(order)
        if action == 'retry' and order.status in ('applying', 'ready'):
            return order_out(order)
        if account.active_order_id != order.id:
            raise HTTPException(409, 'capacity_order_closed')
        if action == 'cancel':
            if order.status != 'pending_payment':
                raise HTTPException(409, 'capacity_cannot_cancel_paid_order')
            order.status, order.stage = 'cancelled', 'cancelled'
            order.completed_at, account.active_order_id = now(), None
        else:
            expected = 'pending_payment' if action == 'confirm-test' else 'failed'
            if order.status != expected or (order.error_code and 'requires_review' in order.error_code):
                raise HTTPException(409, 'capacity_action_unavailable')
            order.status, order.payment_status, order.stage = 'applying', 'confirmed', 'checking_storage'
            order.started_at, order.completed_at, order.error_code = now(), None, None
            order.attempt += 1
        audit(db, principal, env, action.replace('-', '_'), order.id)
        db.commit()
        return order_out(order)

    return router
