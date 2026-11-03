"""Recoverable model credit application and owner wallet observation."""
from datetime import timezone
import logging
import time

from cryptography.fernet import Fernet
import httpx
from sqlalchemy import select

from .ai_policy import effective_policy, provider_catalog
from .database import SessionLocal
from .litellm import LiteLLMError
from .model_billing_provider import ModelBillingGateway, ModelBudgetConflict
from .models import AIConfiguration, AuditEvent, CapacityOrder, ModelCredential, ModelCredit, ModelWallet, UserEnvironment, now
from .settings import get_settings


def apply_credit(sessions, order_id, gateway, settings):
    with sessions.begin() as db:
        order = db.get(CapacityOrder, order_id)
        if not order or order.payment_status != 'confirmed' or order.status != 'applying' or not order.plan.get('model_credit_usd_micros', 0):
            return
        env = db.get(UserEnvironment, order.environment_id, with_for_update=True)
        if not env or env.status in ('suspended', 'deleted'):
            return
        # Serialize absolute budget targets across all orders of the same owner.
        # A committed target awaiting recovery must finish before a second is sampled.
        queued = list(db.scalars(select(CapacityOrder).where(
            CapacityOrder.environment_id == env.id, CapacityOrder.payment_status == 'confirmed',
            CapacityOrder.status == 'applying').order_by(CapacityOrder.created_at, CapacityOrder.id)))
        queued = [item for item in queued if item.plan.get('model_credit_usd_micros', 0)]
        blocked = db.scalar(select(ModelCredit.order_id).where(
            ModelCredit.environment_id == env.id, ModelCredit.status == 'requires_review'))
        if blocked or (queued and queued[0].id != order_id):
            return
        row = db.get(ModelCredit, order_id)
        credential = db.scalar(select(ModelCredential).where(ModelCredential.issuer == env.issuer,
            ModelCredential.subject == env.subject, ModelCredential.status == 'ready'))
        if not credential:
            return
        key = Fernet(settings.model_credential_encryption_key.encode()).decrypt(credential.encrypted_key.encode()).decode()
        if not row:
            row = ModelCredit(order_id=order_id, environment_id=env.id, amount_micros=order.plan['model_credit_usd_micros'], status='pending')
            db.add(row)
            return
        if row.status == 'applied':
            order.evidence = {**order.evidence, 'model_credit_status': 'applied'}
            return
        if row.status == 'requires_review':
            return
        if not row.evidence:
            row.evidence = gateway.targets(env, key, row.amount_micros)
            row.status = 'applying'
            return  # Targets must commit before any provider budget write.
        evidence = dict(row.evidence)
    gateway.apply(env, key, evidence)
    with sessions.begin() as db:
        db.get(UserEnvironment, env.id, with_for_update=True)
        row = db.get(ModelCredit, order_id)
        order = db.get(CapacityOrder, order_id)
        if row.status != 'applied':
            row.status = 'applied'
            order.evidence = {**order.evidence, 'model_credit_status': 'applied'}
            wallet = db.get(ModelWallet, env.id)
            if wallet:
                wallet.observed_at = None
            db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer='internal:billing', subject='billing-controller',
                              action='model_credit.applied', target_id=order_id))


def reconcile(sessions, settings, gateway):
    with sessions() as db:
        orders = list(db.scalars(select(CapacityOrder.id).where(CapacityOrder.status == 'applying', CapacityOrder.payment_status == 'confirmed')))
    for identifier in orders:
        try:
            apply_credit(sessions, identifier, gateway, settings)
        except ModelBudgetConflict:
            with sessions.begin() as db:
                order = db.get(CapacityOrder, identifier, with_for_update=True)
                row = db.get(ModelCredit, identifier)
                if row:
                    row.status = 'requires_review'
                order.status, order.error_code = 'failed', 'capacity_model_credit_requires_review'
                order.evidence = {**order.evidence, 'model_credit_status': 'requires_review'}
        except Exception:
            # Ambiguous writes retain their committed absolute target for recovery.
            pass


def sample_wallets(sessions, settings, gateway):
    with sessions() as db:
        envs = list(db.scalars(select(UserEnvironment).where(UserEnvironment.status.not_in(('deleted', 'suspended')))))
    for env in envs:
        with sessions() as db:
            previous = db.get(ModelWallet, env.id)
            if previous and previous.observed_at and (now() - previous.observed_at.replace(tzinfo=timezone.utc)).total_seconds() < 30:
                continue
            credential = db.scalar(select(ModelCredential).where(ModelCredential.issuer == env.issuer, ModelCredential.subject == env.subject, ModelCredential.status == 'ready'))
            if not credential:
                continue
            policy = effective_policy(db, settings)
        try:
            key = Fernet(settings.model_credential_encryption_key.encode()).decrypt(credential.encrypted_key.encode()).decode()
            gateway.synchronize_models(env, key, policy['allowed_models'])
            sample = gateway.sample(env, key)
        except Exception:
            sample = None
        with sessions.begin() as db:
            db.get(UserEnvironment, env.id, with_for_update=True)
            row = db.get(ModelWallet, env.id)
            if row is None:
                row = ModelWallet(environment_id=env.id)
                db.add(row)
            if sample is None:
                row.status = 'stale' if row.observed_at else 'unknown'
            else:
                row.sample, row.observed_at, row.status = sample, now(), 'ok'


def main():
    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    with httpx.Client(base_url=settings.litellm_url, timeout=10, trust_env=False) as client:
        gateway = ModelBillingGateway(client, settings.litellm_master_key,
                                      tuple(value.strip() for value in settings.litellm_models.split(',') if value.strip()))
        while True:
            try:
                catalog = provider_catalog(settings)
                with SessionLocal.begin() as db:
                    row = db.get(AIConfiguration, 'default', with_for_update=True)
                    if row is None:
                        row = AIConfiguration(id='default', revision=1, config=effective_policy(db, settings))
                        db.add(row)
                    row.catalog, row.observed_at = catalog, now()
            except Exception as error:
                logging.warning('Model catalog observation needs inspection (%s)', type(error).__name__)
            try:
                reconcile(SessionLocal, settings, gateway)
                sample_wallets(SessionLocal, settings, gateway)
            except Exception as error:
                logging.warning('Model billing observation needs inspection (%s)', type(error).__name__)
            time.sleep(5)


if __name__ == '__main__':
    main()
