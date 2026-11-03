"""Shared PostgreSQL lifecycle, connection Secret delivery and actual size samples."""
from pathlib import Path
from datetime import timezone
import logging

from cryptography.fernet import Fernet
from sqlalchemy import select

from .models import ProjectDatabase, UserEnvironment, WorkspaceFileService, now
from .project_databases import names, connection_profile
from .project_database_provider import PostgresProjectProvider, DatabaseOwnershipError

logger = logging.getLogger(__name__)


def secret_path(env, row):
    return f'api/v1/namespaces/codechat-user-{env.identity_hash[:40]}/secrets/{names(row)[2]}'


def reconcile_databases(sessions, cluster, settings, provider=None):
    if not settings.project_databases_enabled:
        return
    provider = provider or PostgresProjectProvider(settings.project_database_provisioner_url)
    with sessions() as db:
        identifiers = list(db.scalars(select(ProjectDatabase.id).where(ProjectDatabase.status != 'deleted')))
    for identifier in identifiers:
        try:
            reconcile_database(sessions, identifier, cluster, settings, provider)
        except Exception as error:
            # Never put a driver diagnostic (which can contain DSNs/SQL) in logs.
            code = ('database_ownership_requires_review' if isinstance(error, DatabaseOwnershipError)
                    else 'database_operation_failed')
            with sessions.begin() as db:
                row = db.get(ProjectDatabase, identifier, with_for_update=True)
                if row and row.status != 'deleted':
                    if row.status == 'ready' and not isinstance(error, DatabaseOwnershipError):
                        row.error_code = 'database_measurement_unavailable'
                    else:
                        row.status, row.error_code = 'failed', code
            logger.warning('Project database=%s (%s)', identifier, code)


def reconcile_database(sessions, identifier, cluster, settings, provider):
    with sessions.begin() as db:
        row = db.scalar(select(ProjectDatabase).where(ProjectDatabase.id == identifier).with_for_update(skip_locked=True))
        if not row or row.status in ('deleted', 'failed'):
            return
        env = db.get(UserEnvironment, row.environment_id)
        files = db.get(WorkspaceFileService, env.id)
        if not files or files.status != 'ready' or files.resources.get('RoleBinding', {}).get('state') != 'created':
            return
        database, owner, secret = names(row)
        if row.status == 'deleting':
            provider.delete(row.generation, env.identity_hash, database, owner)
            if row.resources.get('secret_uid'):
                actual = cluster.get(secret_path(env, row))
                if actual:
                    if actual['metadata']['uid'] != row.resources['secret_uid']:
                        raise DatabaseOwnershipError()
                    cluster.delete(secret_path(env, row), row.resources['secret_uid'])
                    return
            elif row.resources.get('secret_state') == 'inflight':
                raise DatabaseOwnershipError()
            row.status, row.error_code = 'deleted', None
            row.used_bytes, row.observed_at = 0, now()
            return
        if row.status == 'requested':
            password = Fernet(settings.model_credential_encryption_key.encode()).decrypt(row.encrypted_password.encode()).decode()
            evidence = provider.ensure(row.generation, env.identity_hash, database, owner, password)
            row.resources = {**row.resources, 'provider': evidence}
            row.status, row.error_code = 'provisioning', None
            return
        if row.status == 'provisioning':
            if row.resources.get('secret_state') == 'inflight':
                raise DatabaseOwnershipError()
            if row.resources.get('secret_uid'):
                actual = cluster.get(secret_path(env, row))
                if not actual or actual['metadata']['uid'] != row.resources['secret_uid']:
                    raise DatabaseOwnershipError()
                row.status = 'ready'
                return
            row.resources = {**row.resources, 'secret_state': 'inflight'}
            # Persist the create-only write journal before submitting to Kubernetes.
            db.commit()
            document = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {
                'name': secret, 'namespace': 'codechat-user-' + env.identity_hash[:40],
                'labels': {'app.kubernetes.io/managed-by': 'platform-control', 'codechat.io/database-id': row.id,
                           'codechat.io/user-hash': env.identity_hash[:40]}}, 'type': 'Opaque',
                'stringData': {**connection_profile(row, settings), 'ca.crt': Path(settings.project_database_ca).read_text()}}
            actual = cluster.create(document)
            uid = actual['metadata'].get('uid')
            if not uid or actual['metadata']['name'] != secret:
                raise DatabaseOwnershipError()
            with sessions.begin() as next_db:
                current = next_db.get(ProjectDatabase, identifier, with_for_update=True)
                if current.generation != row.generation or current.status != 'provisioning':
                    raise DatabaseOwnershipError()
                current.resources = {**current.resources, 'secret_uid': uid, 'secret_state': 'created'}
                current.status, current.error_code = 'ready', None
            return
        if row.status == 'ready':
            if row.observed_at and (now() - row.observed_at.replace(tzinfo=timezone.utc)).total_seconds() < 30:
                return
            used = provider.size(row.generation, env.identity_hash, database, owner)
            if type(used) is not int or used < 0:
                raise ValueError('Invalid database size')
            row.used_bytes, row.observed_at, row.error_code = used, now(), None
