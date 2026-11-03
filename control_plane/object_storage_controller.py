"""One sealed bucket and restricted connection Secret per existing personal space."""
import secrets
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy import select

from .models import UserEnvironment, UserObjectStorage, WorkspaceFileService
from .object_storage import connection_profile, secret_name
from .object_storage_provider import ObjectOwnershipError


def reconcile_objects(sessions, cluster, settings, provider):
    with sessions() as db:
        environments = list(db.scalars(select(UserEnvironment).where(UserEnvironment.status.not_in(('deleted', 'suspended')))))
    for env in environments:
        try:
            reconcile_object(sessions, env.id, cluster, settings, provider)
        except Exception as error:
            with sessions.begin() as db:
                row = db.get(UserObjectStorage, env.id, with_for_update=True)
                if row:
                    row.status = 'requires_review' if isinstance(error, ObjectOwnershipError) else 'failed'
                    row.error_code = 'object_storage_identity_requires_review' if isinstance(error, ObjectOwnershipError) else 'object_storage_provision_failed'


def reconcile_object(sessions, env_id, cluster, settings, provider):
    with sessions.begin() as db:
        env = db.get(UserEnvironment, env_id, with_for_update=True)
        if not env or env.status in ('deleted', 'suspended'):
            return
        service = db.get(WorkspaceFileService, env_id)
        if not service or service.status != 'ready' or service.resources.get('RoleBinding', {}).get('state') != 'created':
            return
        row = db.get(UserObjectStorage, env_id)
        if row is None:
            if not settings.object_storage_enabled:
                return
            generation = str(uuid4())
            row = UserObjectStorage(environment_id=env_id, generation=generation,
                bucket_name='cc-' + env.identity_hash[:40] + '-' + generation.replace('-', '')[:12],
                access_key='cc' + secrets.token_hex(9),
                encrypted_secret=Fernet(settings.model_credential_encryption_key.encode()).encrypt(secrets.token_urlsafe(30).encode()).decode(),
                status='preparing', resources={'phase': 'bucket', 'endpoint': settings.object_storage_endpoint,
                    'budget_bytes': settings.object_storage_budget_mib * 1024 ** 2})
            db.add(row)
            return
        if row.status in ('ready', 'failed', 'requires_review'):
            return
        phase = row.resources['phase']
        if phase == 'bucket':
            provider.ensure_bucket(row)
            row.resources = {**row.resources, 'phase': 'key'}
        elif phase == 'key':
            secret = Fernet(settings.model_credential_encryption_key.encode()).decrypt(row.encrypted_secret.encode()).decode()
            provider.ensure_key(row, secret)
            row.resources = {**row.resources, 'phase': 'quota'}
        elif phase == 'quota':
            provider.ensure_quota(row)
            row.resources = {**row.resources, 'phase': 'secret'}
        elif phase == 'secret':
            if row.resources.get('secret_state') == 'inflight':
                raise ObjectOwnershipError()
            row.resources = {**row.resources, 'secret_state': 'inflight'}
            db.commit()
            name = secret_name(row)
            document = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque', 'metadata': {
                'name': name, 'namespace': 'codechat-user-' + env.identity_hash[:40],
                'labels': {'app.kubernetes.io/managed-by': 'platform-control', 'codechat.io/object-storage-id': row.generation,
                           'codechat.io/user-hash': env.identity_hash[:40]}}, 'stringData': connection_profile(row, settings)}
            actual = cluster.create(document)
            if not actual['metadata'].get('uid') or actual['metadata']['name'] != name:
                raise ObjectOwnershipError()
            with sessions.begin() as next_db:
                current = next_db.get(UserObjectStorage, env_id, with_for_update=True)
                if current.generation != row.generation or current.status != 'preparing':
                    raise ObjectOwnershipError()
                current.resources = {**current.resources, 'secret_uid': actual['metadata']['uid'], 'secret_state': 'created', 'phase': 'ready'}
                current.status, current.error_code = 'ready', None
