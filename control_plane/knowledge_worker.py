"""Restart-safe index reconciliation; response loss is verified before any retry."""
import time
from datetime import timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, or_

from .agent_tasks import unseal
from .knowledge import KnowledgeClient, revision_scope
from .models import AIProjectBinding, ProjectKnowledge, KnowledgeLibrary, UserEnvironment, now


def run_once(sessions, settings, client=None):
    client = client or KnowledgeClient(settings)
    with sessions() as db:
        candidate = db.scalar(select(ProjectKnowledge).where(
            ProjectKnowledge.state.in_(('queued', 'indexing', 'deleting')),
            or_(ProjectKnowledge.lease_until.is_(None), ProjectKnowledge.lease_until < now()))
            .order_by(ProjectKnowledge.created_at).limit(1))
        if not candidate:
            return False
        # Same lock order as API writers: environment, revision. No provider call under these locks.
        env = db.get(UserEnvironment, candidate.environment_id, with_for_update=True)
        db.refresh(candidate, with_for_update=True)
        if (candidate.state not in ('queued', 'indexing', 'deleting') or
                (candidate.lease_until and candidate.lease_until.replace(tzinfo=timezone.utc) > now())):
            return True
        if env.status == 'suspended' and candidate.state != 'deleting':
            candidate.state, candidate.error_code = 'failed', 'knowledge_account_suspended'
            db.commit()
            return True
        deleting = candidate.state == 'deleting'
        token = str(uuid4())
        candidate.state = 'deleting' if deleting else 'indexing'
        candidate.lease_token, candidate.lease_until = token, now()+timedelta(seconds=settings.knowledge_request_seconds+60)
        candidate.attempts += 1
        candidate.updated_at = now()
        scope, revision, digest = revision_scope(candidate), candidate.id, candidate.digest
        expected = (candidate.source_bytes, candidate.index_bytes, candidate.chunks)
        encrypted = candidate.encrypted_source
        db.commit()
    state, code = ('deleting', 'knowledge_cleanup_pending') if deleting else ('failed', 'knowledge_index_failed')
    try:
        if deleting:
            if client.call('DELETE', scope, revision).get('deleted') is not True:
                raise ValueError('delete_unconfirmed')
            state, code = 'deleted', None
        elif expected[2] == 0:
            state, code = 'ready', None
        else:
            # The store commits metadata and all chunks atomically. A previous lost reply is recoverable.
            try:
                result = client.call('GET', scope, revision)
            except Exception:
                result = client.call('PUT', scope, revision,
                    {'digest': digest, 'files': unseal(encrypted, settings)})
            if (result.get('digest') != digest or
                    tuple(result.get(k) for k in ('source_bytes', 'index_bytes', 'chunks')) != expected):
                raise ValueError('index_unverified')
            state, code = 'ready', None
    except Exception:
        # No submitted text, credentials, or provider exception is persisted/logged.
        pass
    with sessions() as db:
        row = db.get(ProjectKnowledge, revision)
        db.get(UserEnvironment, row.environment_id, with_for_update=True)
        db.refresh(row, with_for_update=True)
        if row.lease_token != token or row.state != ('deleting' if deleting else 'indexing'):
            return True
        row.state, row.error_code, row.updated_at = state, code, now()
        row.lease_token = None
        row.lease_until = now()+timedelta(seconds=30) if state == 'deleting' else None
        if state == 'deleted':
            row.encrypted_source, row.manifest, row.source_bytes, row.index_bytes = '', [], 0, 0
        if state == 'ready':
            if row.library_id:
                library = db.get(KnowledgeLibrary, row.library_id)
                if library.state == 'active' and library.head_revision_id == row.id:
                    library.active_revision_id, library.updated_at = row.id, now()
                    for binding in db.scalars(select(AIProjectBinding).where(AIProjectBinding.knowledge_library_id == library.id)):
                        binding.knowledge_revision_id = row.id
            else:
                binding = db.get(AIProjectBinding, scope)
                current = db.get(ProjectKnowledge, binding.knowledge_revision_id) if binding and binding.knowledge_revision_id else None
                if binding and (not current or row.created_at >= current.created_at):
                    binding.knowledge_revision_id = row.id
        if state == 'deleted' and row.library_id:
            library = db.get(KnowledgeLibrary, row.library_id)
            db.flush()
            remaining = db.scalar(select(ProjectKnowledge.id).where(ProjectKnowledge.library_id == library.id,
                ProjectKnowledge.state != 'deleted').limit(1))
            if library.state == 'deleting' and not remaining:
                library.state, library.updated_at = 'deleted', now()
        db.commit()
    return True


def main():
    from .database import SessionLocal
    from .settings import get_settings
    settings = get_settings()
    while True:
        try:
            if settings.knowledge_enabled:
                run_once(SessionLocal, settings)
        except Exception:
            print('knowledge reconciliation unavailable', flush=True)
        time.sleep(settings.knowledge_poll_seconds)


if __name__ == '__main__':
    main()
