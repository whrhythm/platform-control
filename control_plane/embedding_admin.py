"""Immutable embedding configurations and an administrator-selected recommendation."""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, update

from .agent_tasks import seal, unseal
from .database import get_db
from .knowledge import KnowledgeClient
from .knowledge_store import embedding_endpoint
from .mcp_catalog import credential_value
from .models import AuditEvent, EmbeddingPreference, EmbeddingProfile
from .settings import get_settings

LOCAL = {'id': 'local', 'model': 'BAAI/bge-small-zh-v1.5', 'url': None}


class ExternalInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    url: str = Field(max_length=500)
    model: str = Field(min_length=1, max_length=200)
    secret: SecretStr | None = None
    request_dimensions: bool = False


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile: UUID | None
    version: int = Field(ge=0)


def selected_profile(db):
    return db.get(EmbeddingPreference, 1)


def describe(row):
    return {'id': row.id, 'model': row.model, 'url': row.url} if row else LOCAL


def runtime_profile(db, settings, identifier):
    if not identifier:
        return {}
    row = db.get(EmbeddingProfile, identifier)
    if row is None:
        raise ValueError('knowledge_embedding_profile_missing')
    return {'embedding': {
        **describe(row), 'request_dimensions': row.request_dimensions,
        'secret': unseal(row.encrypted_secret, settings) if row.encrypted_secret else '',
    }}


def create_router(admin_dependency):
    router = APIRouter(prefix='/v1/admin/knowledge-embedding', tags=['administration'])

    def state(db):
        preference = selected_profile(db)
        return {'profiles': [LOCAL, *[describe(row) for row in db.scalars(
            select(EmbeddingProfile).order_by(EmbeddingProfile.created_at.desc()))]],
            'selected': preference.profile_id or 'local', 'version': preference.version}

    @router.get('')
    def listing(principal=Depends(admin_dependency), db=Depends(get_db)):
        return state(db)

    @router.post('/profiles', status_code=201)
    def create(body: ExternalInput, principal=Depends(admin_dependency), db=Depends(get_db)):
        settings = get_settings()
        if not settings.knowledge_enabled:
            raise HTTPException(404, 'knowledge_disabled')
        try:
            url = embedding_endpoint(body.url)
        except ValueError:
            raise HTTPException(422, 'knowledge_embedding_url_invalid') from None
        secret = credential_value(body.secret.get_secret_value()) if body.secret else ''
        identifier = str(uuid4())
        payload = {'id': identifier, 'url': url, 'model': body.model,
                   'secret': secret, 'request_dimensions': body.request_dimensions}
        try:
            KnowledgeClient(settings).probe(payload)
        except Exception:
            raise HTTPException(503, 'knowledge_embedding_probe_failed') from None
        db.add(EmbeddingProfile(id=identifier, model=body.model, url=url,
                               request_dimensions=body.request_dimensions,
                               encrypted_secret=seal(secret, settings) if secret else None))
        db.add(AuditEvent(tenant_id='platform', issuer=principal.issuer, subject=principal.subject,
                          action='embedding_profile_verified', target_id=identifier))
        db.commit()
        return state(db)

    @router.put('')
    def select_profile(body: Selection, principal=Depends(admin_dependency), db=Depends(get_db)):
        profile_id = str(body.profile) if body.profile else None
        if profile_id and not db.get(EmbeddingProfile, profile_id):
            raise HTTPException(404, 'knowledge_embedding_profile_missing')
        changed = db.execute(update(EmbeddingPreference).where(
            EmbeddingPreference.id == 1, EmbeddingPreference.version == body.version).values(
            profile_id=profile_id, version=EmbeddingPreference.version+1))
        if changed.rowcount != 1:
            raise HTTPException(409, 'knowledge_embedding_selection_changed')
        db.add(AuditEvent(tenant_id='platform', issuer=principal.issuer, subject=principal.subject,
                          action='embedding_profile_selected', target_id=profile_id or 'local'))
        db.commit()
        return state(db)

    return router
