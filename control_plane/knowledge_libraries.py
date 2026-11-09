"""Owner-scoped knowledge assets; immutable revisions and optimistic file edits."""
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func

from .agent_tasks import seal, unseal
from .database import get_db
from .embedding_admin import describe, runtime_profile, selected_profile
from .knowledge import KnowledgeClient, Source, public, reserve_storage, revision_scope
from .knowledge_format import prepare
from .models import (KnowledgeLibrary, ProjectKnowledge, AIProjectBinding, AIProjectTrial, EmbeddingProfile,
                     WorkspaceProject, AuditEvent, now)
from .settings import get_settings
from .workspace_gateway import own_environment


class Create(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: UUID
    name: str = Field(min_length=1, max_length=80)


class Rename(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=1)


class Edit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    expected_revision_id: UUID | None
    action: Literal['upsert', 'rename', 'remove', 'rebuild']
    embedding_profile: str | None = Field(default=None, max_length=36)
    files: list[Source] | None = Field(default=None, min_length=1, max_length=10)
    name: str | None = Field(default=None, min_length=1, max_length=124)
    new_name: str | None = Field(default=None, min_length=1, max_length=124)


class Bind(BaseModel):
    model_config = ConfigDict(extra='forbid')
    library_id: UUID | None
    expected_library_id: UUID | None
    native_owner_id: str = Field(pattern=r'^[0-9a-f]{24}$')


class Cleanup(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision_id: UUID


class LibrarySearch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=2000)


def audit(db, env, principal, action, target):
    db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer,
        subject=principal.subject, action='knowledge.'+action, target_id=target))


def legacy_library(db, binding):
    """Old project imports lazily receive a library; IDs and vector scopes stay stable."""
    if binding.knowledge_library_id:
        return db.get(KnowledgeLibrary, binding.knowledge_library_id)
    library = db.get(KnowledgeLibrary, binding.project_id)
    if not library:
        settings = get_settings()
        count = db.scalar(select(func.count()).select_from(KnowledgeLibrary).where(
            KnowledgeLibrary.environment_id == binding.environment_id, KnowledgeLibrary.state != 'deleted'))
        if count >= settings.knowledge_library_limit:
            raise HTTPException(409, 'knowledge_library_limit')
        project = db.get(WorkspaceProject, binding.project_id)
        library = KnowledgeLibrary(id=binding.project_id, environment_id=binding.environment_id,
            request_id=binding.project_id, name=project.name,
            active_revision_id=binding.knowledge_revision_id)
        db.add(library)
        db.flush()
    binding.knowledge_library_id = library.id
    return library


def references(db, library):
    projects = list(db.execute(select(WorkspaceProject.id, WorkspaceProject.name)
        .join(AIProjectBinding, AIProjectBinding.project_id == WorkspaceProject.id)
        .where(AIProjectBinding.knowledge_library_id == library.id)))
    trials = db.scalar(select(func.count()).select_from(AIProjectTrial)
        .join(ProjectKnowledge, AIProjectTrial.knowledge_revision_id == ProjectKnowledge.id)
        .where(ProjectKnowledge.library_id == library.id))
    return {'projects': [{'id': p.id, 'name': p.name} for p in projects], 'fixed_trials': trials}


def view(db, library, details=False):
    head = db.get(ProjectKnowledge, library.head_revision_id) if library.head_revision_id else None
    result = {k: getattr(library, k) for k in ('id', 'name', 'state', 'version',
        'head_revision_id', 'active_revision_id', 'created_at', 'updated_at')}
    result['head'] = public(head) if head else None
    if details:
        settings = get_settings()
        result['limits'] = {'files': settings.knowledge_file_limit, 'file_bytes': settings.knowledge_file_bytes,
                            'revisions': settings.knowledge_revision_limit}
        result['references'] = references(db, library)
        preference = selected_profile(db)
        active = db.get(ProjectKnowledge, library.active_revision_id) if library.active_revision_id else None
        result['embedding'] = {
            'active': describe(db.get(EmbeddingProfile, active.embedding_profile_id)
                               if active and active.embedding_profile_id else None),
            'recommended': describe(db.get(EmbeddingProfile, preference.profile_id)
                                    if preference and preference.profile_id else None),
        }
        result['revisions'] = [public(r) for r in db.scalars(select(ProjectKnowledge)
            .where(ProjectKnowledge.library_id == library.id, ProjectKnowledge.state != 'deleted')
            .order_by(ProjectKnowledge.created_at.desc(), ProjectKnowledge.id.desc()))]
    return result


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace', tags=['knowledge-libraries'])

    def owner(db, principal):
        settings = get_settings()
        if not settings.ai_projects_enabled or not settings.knowledge_enabled:
            raise HTTPException(404, 'knowledge_disabled')
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        if env.status == 'suspended':
            raise HTTPException(403, 'knowledge_account_suspended')
        return env, settings

    def owned(db, principal, identifier, include_removed=False):
        env, settings = owner(db, principal)
        library = db.get(KnowledgeLibrary, str(identifier))
        if not library or library.environment_id != env.id or (not include_removed and library.state != 'active'):
            raise HTTPException(404, 'knowledge_library_not_found')
        return env, settings, library

    @router.get('/knowledge-libraries')
    def listing(principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings = owner(db, principal)
        rows = db.scalars(select(KnowledgeLibrary).where(KnowledgeLibrary.environment_id == env.id,
            KnowledgeLibrary.state == 'active').order_by(KnowledgeLibrary.created_at, KnowledgeLibrary.id))
        return {'libraries': [view(db, row) for row in rows], 'limits': {
            'libraries': settings.knowledge_library_limit, 'files': settings.knowledge_file_limit,
            'file_bytes': settings.knowledge_file_bytes}}

    @router.post('/knowledge-libraries', status_code=201)
    def create(body: Create, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings = owner(db, principal)
        row = db.scalar(select(KnowledgeLibrary).where(KnowledgeLibrary.environment_id == env.id,
            KnowledgeLibrary.request_id == str(body.request_id)))
        if row:
            if row.name != body.name or row.state != 'active':
                raise HTTPException(409, 'knowledge_request_conflict')
            return view(db, row, True)
        count = db.scalar(select(func.count()).select_from(KnowledgeLibrary).where(
            KnowledgeLibrary.environment_id == env.id, KnowledgeLibrary.state != 'deleted'))
        if count >= settings.knowledge_library_limit:
            raise HTTPException(409, 'knowledge_library_limit')
        row = KnowledgeLibrary(id=str(uuid4()), environment_id=env.id, request_id=str(body.request_id), name=body.name)
        db.add(row)
        audit(db, env, principal, 'library_created', row.id)
        db.commit()
        return view(db, row, True)

    @router.get('/knowledge-libraries/{library_id}')
    def detail(library_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        _, _, row = owned(db, principal, library_id)
        return view(db, row, True)

    @router.post('/knowledge-libraries/{library_id}/search')
    def search(library_id: UUID, body: LibrarySearch, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings, library = owned(db, principal, library_id)
        row = db.get(ProjectKnowledge, library.active_revision_id) if library.active_revision_id else None
        if (not row or row.library_id != library.id or row.environment_id != env.id
                or row.state != 'ready' or row.chunks == 0):
            return {'revision_id': None, 'sources': []}
        scope, identifier = revision_scope(row), row.id
        try:
            profile = runtime_profile(db, settings, row.embedding_profile_id)
            db.commit()
            result = KnowledgeClient(settings).call('POST', scope, identifier,
                {'query': body.query, 'k': 5, **profile}, query=True)
        except Exception:
            raise HTTPException(503, 'knowledge_search_unavailable') from None
        db.expire_all()
        _, _, library = owned(db, principal, library_id)
        if library.active_revision_id != identifier or row.state != 'ready':
            raise HTTPException(409, 'knowledge_revision_changed')
        return {'revision_id': identifier, 'sources': result['sources']}

    @router.post('/knowledge-libraries/{library_id}/rename')
    def rename(library_id: UUID, body: Rename, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, _, row = owned(db, principal, library_id)
        if row.version != body.expected_version:
            raise HTTPException(409, 'knowledge_version_conflict')
        row.name, row.version, row.updated_at = body.name, row.version+1, now()
        audit(db, env, principal, 'library_renamed', row.id)
        db.commit()
        return view(db, row, True)

    @router.post('/knowledge-libraries/{library_id}/files', status_code=202)
    def edit(library_id: UUID, body: Edit, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, settings, library = owned(db, principal, library_id)
        # Request digest covers intent, including expected revision; retries never reapply a mutation.
        from hashlib import sha256
        if body.action != 'rebuild' and body.embedding_profile is not None:
            raise HTTPException(400, 'knowledge_invalid_request')
        excluded = {'embedding_profile'} if body.action != 'rebuild' else set()
        intent = sha256(body.model_dump_json(exclude=excluded).encode()).hexdigest()
        existing = db.scalar(select(ProjectKnowledge).where(ProjectKnowledge.library_id == library.id,
            ProjectKnowledge.request_id == str(body.request_id)))
        if existing:
            if existing.edit_digest != intent:
                raise HTTPException(409, 'knowledge_request_conflict')
            return view(db, library, True)
        if library.head_revision_id != (str(body.expected_revision_id) if body.expected_revision_id else None):
            raise HTTPException(409, 'knowledge_version_conflict')
        previous = db.get(ProjectKnowledge, library.head_revision_id) if library.head_revision_id else None
        profile_id = previous.embedding_profile_id if previous else None
        files = unseal(previous.encrypted_source, settings) if previous else []
        by_name = {f['name']: f for f in files}
        if body.action == 'rebuild' and body.files is None and not body.name and not body.new_name:
            preference = selected_profile(db)
            recommended = preference.profile_id if preference else None
            if body.embedding_profile != (recommended or 'local'):
                raise HTTPException(409, 'knowledge_embedding_selection_changed')
            if previous and previous.state in ('queued', 'indexing'):
                raise HTTPException(409, 'knowledge_version_conflict')
            profile_id = recommended
        elif body.action == 'upsert' and body.files and not body.name and not body.new_name:
            if len({f.name for f in body.files}) != len(body.files):
                raise HTTPException(400, 'knowledge_invalid_file')
            by_name.update({f.name: f.model_dump() for f in body.files})
        elif body.action in ('remove', 'rename') and body.name and body.files is None:
            if body.name not in by_name:
                raise HTTPException(404, 'knowledge_file_not_found')
            item = by_name.pop(body.name)
            if body.action == 'rename' and body.new_name:
                if body.new_name in by_name:
                    raise HTTPException(409, 'knowledge_file_exists')
                by_name[body.new_name] = {**item, 'name': body.new_name}
            elif body.action != 'remove' or body.new_name:
                raise HTTPException(400, 'knowledge_invalid_request')
        else:
            raise HTTPException(400, 'knowledge_invalid_request')
        files = sorted(by_name.values(), key=lambda f: f['name'])
        try:
            digest, size, index, chunks = prepare(files, settings.knowledge_file_limit,
                settings.knowledge_file_bytes, settings.knowledge_chunk_limit, allow_empty=True)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        count = db.scalar(select(func.count()).select_from(ProjectKnowledge).where(
            ProjectKnowledge.library_id == library.id, ProjectKnowledge.state != 'deleted'))
        if count >= settings.knowledge_revision_limit:
            raise HTTPException(409, 'knowledge_revision_limit')
        reserve_storage(db, env, principal, settings, size+index)
        row = ProjectKnowledge(id=str(uuid4()), library_id=library.id, environment_id=env.id,
            request_id=str(body.request_id), digest=digest, edit_digest=intent, encrypted_source=seal(files, settings),
            embedding_profile_id=profile_id,
            manifest=[{'name': f['name'], 'bytes': len(f['content'].encode())} for f in files],
            source_bytes=size, index_bytes=index, chunks=len(chunks), state='queued')
        db.add(row)
        library.head_revision_id, library.updated_at = row.id, now()
        audit(db, env, principal, 'index_rebuilt' if body.action == 'rebuild' else 'files_changed', row.id)
        db.commit()
        return view(db, library, True)

    @router.get('/knowledge-libraries/{library_id}/source')
    def source(library_id: UUID, revision_id: UUID, name: str = Query(min_length=1, max_length=124),
               principal=Depends(principal_dependency), db=Depends(get_db)):
        _, settings, library = owned(db, principal, library_id)
        row = db.get(ProjectKnowledge, str(revision_id))
        if not row or row.library_id != library.id or row.state in ('deleting', 'deleted'):
            raise HTTPException(404, 'knowledge_not_found')
        for file in unseal(row.encrypted_source, settings):
            if file['name'] == name:
                return {**file, 'revision_id': row.id, 'digest': row.digest}
        raise HTTPException(404, 'knowledge_file_not_found')

    @router.post('/knowledge-libraries/{library_id}/retry')
    def retry(library_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, _, library = owned(db, principal, library_id)
        row = db.get(ProjectKnowledge, library.head_revision_id) if library.head_revision_id else None
        if not row or row.state not in ('failed', 'queued', 'indexing', 'ready'):
            raise HTTPException(409, 'knowledge_not_retryable')
        if row.state == 'failed':
            row.state, row.error_code, row.attempts, row.updated_at = 'queued', None, 0, now()
            audit(db, env, principal, 'retry', row.id)
            db.commit()
        return view(db, library, True)

    @router.post('/knowledge-libraries/{library_id}/remove')
    def remove(library_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, _, library = owned(db, principal, library_id, include_removed=True)
        if library.state in ('deleting', 'deleted'):
            return {'id': library.id, 'state': library.state}
        refs = references(db, library)
        if refs['projects'] or refs['fixed_trials']:
            raise HTTPException(409, 'knowledge_library_in_use')
        rows = list(db.scalars(select(ProjectKnowledge).where(ProjectKnowledge.library_id == library.id,
            ProjectKnowledge.state != 'deleted')))
        library.state, library.updated_at = ('deleting' if rows else 'deleted'), now()
        for row in rows:
            row.state, row.updated_at, row.lease_token, row.lease_until = 'deleting', now(), None, None
        audit(db, env, principal, 'library_remove_requested', library.id)
        db.commit()
        return {'id': library.id, 'state': library.state}

    @router.post('/knowledge-libraries/{library_id}/cleanup')
    def cleanup(library_id: UUID, body: Cleanup, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, _, library = owned(db, principal, library_id)
        row = db.get(ProjectKnowledge, str(body.revision_id))
        if not row or row.library_id != library.id:
            raise HTTPException(404, 'knowledge_not_found')
        pinned = db.scalar(select(AIProjectTrial.id).where(AIProjectTrial.knowledge_revision_id == row.id).limit(1))
        if row.id in (library.head_revision_id, library.active_revision_id) or pinned:
            raise HTTPException(409, 'knowledge_release_in_use')
        if row.state not in ('deleted', 'deleting'):
            row.state, row.lease_token, row.lease_until, row.updated_at = 'deleting', None, None, now()
            audit(db, env, principal, 'revision_cleanup_requested', row.id)
            db.commit()
        return view(db, library, True)

    @router.get('/ai-projects/{project_id}/knowledge-library')
    def selected(project_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, _ = owner(db, principal)
        binding = db.get(AIProjectBinding, str(project_id))
        if not binding or binding.environment_id != env.id or binding.state != 'ready':
            raise HTTPException(404, 'knowledge_project_not_found')
        library = db.get(KnowledgeLibrary, binding.knowledge_library_id) if binding.knowledge_library_id else None
        return {'library': view(db, library) if library and library.state == 'active' else None}

    @router.post('/ai-projects/{project_id}/knowledge-library')
    def bind(project_id: UUID, body: Bind, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, _ = owner(db, principal)
        binding = db.get(AIProjectBinding, str(project_id))
        if not binding or binding.environment_id != env.id or binding.state != 'ready':
            raise HTTPException(404, 'knowledge_project_not_found')
        if body.native_owner_id != binding.native_owner_id:
            raise HTTPException(403, 'knowledge_owner_mismatch')
        selected_id = str(body.library_id) if body.library_id else None
        if binding.knowledge_library_id == selected_id:
            return selected(project_id, principal, db)
        expected = str(body.expected_library_id) if body.expected_library_id else None
        if binding.knowledge_library_id != expected:
            raise HTTPException(409, 'knowledge_version_conflict')
        library = owned(db, principal, selected_id)[2] if selected_id else None
        binding.knowledge_library_id = selected_id
        binding.knowledge_revision_id = library.active_revision_id if library else None
        audit(db, env, principal, 'library_bound', binding.project_id)
        db.commit()
        return {'library': view(db, library) if library else None}

    return router
