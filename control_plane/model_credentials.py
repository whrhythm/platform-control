"""Durable, idempotent per-user LiteLLM credential provisioning."""
import secrets

from cryptography.fernet import Fernet, InvalidToken
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import Principal
from .litellm import LiteLLMError, LiteLLMGateway, model_user_id
from .models import AuditEvent, ModelCredential
from .settings import Settings


def provision_model_key(db: Session, principal: Principal, settings: Settings, tenant_id: str) -> str:
    models = tuple(model.strip() for model in settings.litellm_models.split(',') if model.strip())
    if not all((settings.litellm_url, settings.litellm_master_key, models, settings.model_credential_encryption_key)):
        raise LiteLLMError('provisioning_not_configured')
    try:
        cipher = Fernet(settings.model_credential_encryption_key.encode())
    except (ValueError, TypeError):
        raise LiteLLMError('provisioning_not_configured') from None

    identity = (ModelCredential.issuer == principal.issuer, ModelCredential.subject == principal.subject)
    record = db.scalar(select(ModelCredential).where(*identity))
    if record is None:
        candidate = 'sk-' + secrets.token_urlsafe(48)
        db.add(ModelCredential(issuer=principal.issuer, subject=principal.subject,
                               encrypted_key=cipher.encrypt(candidate.encode()).decode(), status='pending'))
        try:
            db.commit()  # Candidate must survive a crash before any external write.
        except IntegrityError:
            db.rollback()  # Another login inserted the same identity.

    record = db.scalar(select(ModelCredential).where(*identity).with_for_update())
    if record is None or record.status not in ('pending', 'ready'):
        db.rollback()
        raise LiteLLMError('credential_disabled')
    try:
        candidate = cipher.decrypt(record.encrypted_key.encode()).decode()
    except (InvalidToken, ValueError, UnicodeError):
        db.rollback()
        raise LiteLLMError('credential_decryption_failed') from None
    try:
        with httpx.Client(base_url=settings.litellm_url, timeout=10.0, trust_env=False) as client:
            gateway = LiteLLMGateway(client, settings.litellm_master_key, models)
            user_id = gateway.ensure_user(principal.issuer, principal.subject, allow_create=record.status == 'pending')
            gateway.ensure_key(user_id, candidate, allow_create=record.status == 'pending')
        created = record.status == 'pending'
        record.status = 'ready'
        if created:
            db.add(AuditEvent(tenant_id=tenant_id, issuer=principal.issuer, subject=principal.subject,
                              action='model_credential.created', target_id=model_user_id(principal.issuer, principal.subject)))
        db.commit()
        return candidate
    except LiteLLMError:
        db.rollback()
        raise
