"""Project-owned knowledge: durable import, quota reservation, private search."""
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .agent_tasks import seal, unseal
from .database import get_db
from .knowledge_format import prepare, FORMAT
from .models import AIProjectBinding, AuditEvent, CapacityAccount, ProjectKnowledge, now
from .settings import get_settings
from .workspace_gateway import call_files, own_environment


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(max_length=124)
    content: str = Field(max_length=1048576)


class Import(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    native_owner_id: str = Field(pattern=r'^[0-9a-f]{24}$')
    files: list[Source] = Field(min_length=1, max_length=10)


class Search(BaseModel):
    model_config = ConfigDict(extra='forbid')
    native_owner_id: str = Field(pattern=r'^[0-9a-f]{24}$')
    query: str = Field(min_length=1, max_length=2000)
    revision_id: UUID | None = None


def knowledge_usage(db, environment_id):
    return db.scalar(select(func.coalesce(func.sum(ProjectKnowledge.source_bytes+ProjectKnowledge.index_bytes), 0))
        .where(ProjectKnowledge.environment_id == environment_id, ProjectKnowledge.state != 'deleted'))


def latest(db, project_id):
    binding = db.get(AIProjectBinding, project_id)
    row = db.get(ProjectKnowledge, binding.knowledge_revision_id) if binding and binding.knowledge_revision_id else None
    return row if row and row.state == 'ready' else None


def public(row):
    return {k: getattr(row, k) for k in ('id', 'digest', 'manifest', 'source_bytes', 'index_bytes',
        'chunks', 'state', 'error_code', 'created_at', 'updated_at')}


class KnowledgeClient:
    def __init__(self, settings):
        if len(settings.knowledge_service_token) < 32:
            raise ValueError('knowledge_not_configured')
        self.settings = settings

    def call(self, method, scope, revision, payload=None, *, query=False):
        # All scopes/identifiers are resolved from owned records, never arbitrary URLs.
        path = '/v1/'+str(UUID(scope))+'/'+str(UUID(revision))+('/query' if query else '')
        with httpx.Client(base_url=self.settings.knowledge_url, trust_env=False,
                timeout=self.settings.knowledge_query_seconds if query else self.settings.knowledge_request_seconds,
                headers={'Authorization': 'Bearer '+self.settings.knowledge_service_token}) as client:
            response = client.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/ai-projects', tags=['project-knowledge'])

    def owned(db, principal, project_id):
        settings = get_settings()
        if not settings.ai_projects_enabled or not settings.knowledge_enabled:
            raise HTTPException(404, 'knowledge_disabled')
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        if env.status == 'suspended':
            raise HTTPException(403, 'knowledge_account_suspended')
        binding = db.get(AIProjectBinding, str(project_id))
        if not binding or binding.environment_id != env.id or binding.state != 'ready':
            raise HTTPException(404, 'knowledge_project_not_found')
        return env, binding, settings

    def audit(db, env, principal, action, target):
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer,
            subject=principal.subject, action='knowledge.'+action, target_id=target))

    @router.get('/{project_id}/knowledge')
    def listing(project_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        _, _, settings = owned(db, principal, project_id)
        rows = list(db.scalars(select(ProjectKnowledge).where(ProjectKnowledge.project_id == str(project_id),
            ProjectKnowledge.state != 'deleted').order_by(ProjectKnowledge.created_at.desc())))
        active = latest(db, str(project_id))
        return {'revisions': [public(row) for row in rows], 'active_revision_id': active.id if active else None,
                'limits': {'files': settings.knowledge_file_limit, 'file_bytes': settings.knowledge_file_bytes}}

    @router.post('/{project_id}/knowledge', status_code=202)
    def import_files(project_id: UUID, body: Import, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, binding, settings = owned(db, principal, project_id)
        if body.native_owner_id != binding.native_owner_id:
            raise HTTPException(403, 'knowledge_owner_mismatch')
        files = [f.model_dump() for f in body.files]
        try:
            digest, size, index_bytes, chunks = prepare(files, settings.knowledge_file_limit,
                settings.knowledge_file_bytes, settings.knowledge_chunk_limit)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        row = db.scalar(select(ProjectKnowledge).where(ProjectKnowledge.project_id == str(project_id),
            ProjectKnowledge.request_id == str(body.request_id)))
        if row:
            if row.digest != digest:
                raise HTTPException(409, 'knowledge_request_conflict')
            return public(row)
        count = db.scalar(select(func.count()).select_from(ProjectKnowledge).where(
            ProjectKnowledge.project_id == str(project_id), ProjectKnowledge.state != 'deleted'))
        if count >= settings.knowledge_revision_limit:
            raise HTTPException(409, 'knowledge_revision_limit')
        from .capacity import sample_storage
        from .project_versions import version_usage
        observed = call_files(db, principal, settings, 'GET', '/v1/usage', '').json()
        samples = sample_storage(db, env, observed)
        if samples['files'].status != 'ok' or (settings.storage_metering_enabled and
                any(item.status != 'ok' or item.used_bytes is None for item in samples.values())):
            raise HTTPException(503, 'knowledge_usage_unavailable')
        # A metered deployment must not treat an unavailable component as zero.
        used = sum(max(0, item.used_bytes or 0) for item in samples.values())
        used += version_usage(db, env.id)+knowledge_usage(db, env.id)
        account = db.get(CapacityAccount, env.id)
        budget = (account.plan['storage_gib'] if account else env.storage_gib)*1024**3
        if used+size+index_bytes > budget:
            raise HTTPException(507, 'knowledge_storage_full')
        row = ProjectKnowledge(id=str(uuid4()), project_id=str(project_id), environment_id=env.id,
            request_id=str(body.request_id), digest=digest, encrypted_source=seal(files, settings),
            manifest=[{'name': f['name'], 'bytes': len(f['content'].encode())} for f in files],
            source_bytes=size, index_bytes=index_bytes, chunks=len(chunks), state='queued')
        db.add(row)
        audit(db, env, principal, 'requested', row.id)
        db.commit()
        return public(row)

    @router.post('/{project_id}/knowledge/{revision_id}/retry')
    def retry(project_id: UUID, revision_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, binding, _ = owned(db, principal, project_id)
        row = db.get(ProjectKnowledge, str(revision_id))
        if not row or row.project_id != str(project_id):
            raise HTTPException(404, 'knowledge_not_found')
        if row.state == 'failed':
            row.state, row.error_code, row.attempts, row.updated_at = 'queued', None, 0, now()
            audit(db, env, principal, 'retry', row.id)
            db.commit()
        elif row.state not in ('queued', 'indexing', 'ready'):
            raise HTTPException(409, 'knowledge_not_retryable')
        return public(row)

    @router.post('/{project_id}/knowledge/{revision_id}/remove')
    def remove(project_id: UUID, revision_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, binding, _ = owned(db, principal, project_id)
        row = db.get(ProjectKnowledge, str(revision_id))
        if not row or row.project_id != str(project_id):
            raise HTTPException(404, 'knowledge_not_found')
        if row.state not in ('deleted', 'deleting'):
            if binding.knowledge_revision_id == row.id:
                binding.knowledge_revision_id = None
            row.state, row.updated_at, row.lease_token, row.lease_until = 'deleting', now(), None, None
            audit(db, env, principal, 'remove_requested', row.id)
            db.commit()
        return public(row)

    @router.post('/{project_id}/knowledge/search')
    def search(project_id: UUID, body: Search, principal=Depends(principal_dependency), db=Depends(get_db)):
        _, binding, settings = owned(db, principal, project_id)
        if binding.native_owner_id != body.native_owner_id:
            raise HTTPException(403, 'knowledge_owner_mismatch')
        row = db.get(ProjectKnowledge, str(body.revision_id)) if body.revision_id else latest(db, str(project_id))
        if not row or row.project_id != str(project_id) or row.state != 'ready':
            return {'revision_id': None, 'sources': []}
        scope, identifier, digest = row.project_id, row.id, row.digest
        db.commit()  # Do not hold an environment lock while embedding a query.
        try:
            result = KnowledgeClient(settings).call('POST', scope, identifier, {'query': body.query, 'k': 5}, query=True)
        except Exception:
            raise HTTPException(503, 'knowledge_search_unavailable') from None
        # Deletion/revocation during the query must prevent subsequent disclosure.
        owned(db, principal, project_id)
        db.refresh(row)
        if row.state != 'ready':
            raise HTTPException(409, 'knowledge_revision_changed')
        return {'revision_id': identifier, 'digest': digest, 'sources': result['sources']}

    @router.get('/{project_id}/knowledge/{revision_id}/source')
    def source(project_id: UUID, revision_id: UUID, name: str = Query(min_length=1, max_length=124),
               principal=Depends(principal_dependency), db=Depends(get_db)):
        _, _, settings = owned(db, principal, project_id)
        row = db.get(ProjectKnowledge, str(revision_id))
        if not row or row.project_id != str(project_id) or row.state != 'ready':
            raise HTTPException(404, 'knowledge_not_found')
        for file in unseal(row.encrypted_source, settings):
            if file['name'] == name:
                return {'name': name, 'content': file['content'], 'revision_id': row.id, 'digest': row.digest}
        raise HTTPException(404, 'knowledge_not_found')

    return router
