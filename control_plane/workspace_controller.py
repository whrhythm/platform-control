"""Create personal storage and a protected file service without adopting objects."""
from copy import deepcopy
import logging
from pathlib import Path
import secrets
import ssl
import time

from cryptography.fernet import Fernet
import httpx
from sqlalchemy import select

from .database import SessionLocal
from .kubernetes_workspace_provider import KubernetesWorkspaceProvider
from .models import UserEnvironment, WorkspaceProvisioningIntent, WorkspaceFileService
from .settings import get_settings
from .workspace_provisioning import provision_one, observe_and_record_volume, _metadata_match
from .workspace_runtime import file_service_objects, resource_path

logger = logging.getLogger(__name__)


def prepare_service(session_factory, provider, env_id, settings):
    cipher = Fernet(settings.model_credential_encryption_key.encode())
    with session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.id == env_id).with_for_update())
        service = db.get(WorkspaceFileService, env_id)
        if service is None:
            service = WorkspaceFileService(environment_id=env_id,
                encrypted_token=cipher.encrypt(secrets.token_urlsafe(48).encode()).decode(), resources={})
            db.add(service)
            db.flush()
        token = cipher.decrypt(service.encrypted_token.encode()).decode()
        identity_hash = env.identity_hash
    objects = file_service_objects(provider.namespace, identity_hash, settings.workspace_service_image, token)
    for obj in objects:
        key = obj['kind']
        path = resource_path(provider.namespace, key)
        with session_factory.begin() as db:
            service = db.scalar(select(WorkspaceFileService).where(WorkspaceFileService.environment_id == env_id).with_for_update())
            step = service.resources.get(key)
            if step and step['state'] == 'inflight':
                return 'requires_review'
            if not step:
                service.resources = {**service.resources, key: {'state': 'inflight'}}
        if not step:
            actual = provider._request('POST', path, manifest=deepcopy(obj))
            uid = _metadata_match(actual, obj)
            with session_factory.begin() as db:
                service = db.scalar(select(WorkspaceFileService).where(WorkspaceFileService.environment_id == env_id).with_for_update())
                if service.resources.get(key) != {'state': 'inflight'}:
                    raise RuntimeError('File service journal changed')
                service.resources = {**service.resources, key: {'state': 'created', 'uid': uid}}
        else:
            # The controller deliberately has no Secret read permission.
            # Its per-space credential is verified by the authenticated probe.
            if key != 'Secret':
                actual = provider._request('GET', path + '/' + obj['metadata']['name'])
                _metadata_match(actual, obj, namespace_uid=step['uid'])
    try:
        context = ssl.create_default_context(cafile=settings.workspace_service_ca)
        with httpx.Client(verify=context, timeout=5, trust_env=False) as client:
            response = client.get(f'https://workspace-files.{provider.namespace}.svc:8090/v1/directories',
                                  headers={'Authorization': 'Bearer ' + token})
        ready = response.status_code == 200 and isinstance(response.json().get('entries'), list)
    except (httpx.HTTPError, ValueError, OSError):
        ready = False
    with session_factory.begin() as db:
        service = db.get(WorkspaceFileService, env_id)
        service.status = 'ready' if ready else 'preparing'
    return 'ready' if ready else 'preparing'


def reconcile(session_factory, provider, env_id, settings):
    for _ in range(4):
        state = provision_one(session_factory, provider, env_id)
        if state == 'pvc_recorded':
            break
        if state.endswith('_inflight') or state == 'environment_not_eligible':
            return state
    if state != 'pvc_recorded' or observe_and_record_volume(session_factory, provider, env_id) != 'bound':
        return 'waiting_for_storage'
    return prepare_service(session_factory, provider, env_id, settings)


def main():
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.workspace_files_enabled or not settings.workspace_service_image:
        raise RuntimeError('Workspace controller is not configured')
    sa = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    while True:
        with SessionLocal() as db:
            candidates = list(db.scalars(select(UserEnvironment).join(WorkspaceProvisioningIntent)
                                         .where(UserEnvironment.status == 'requested').order_by(UserEnvironment.created_at)))
        for env in candidates:
            provider = KubernetesWorkspaceProvider(api_url='https://kubernetes.default.svc',
                token=(sa / 'token').read_text().strip(), ca_file=str(sa / 'ca.crt'),
                issuer=env.issuer, subject=env.subject, storage_gib=env.storage_gib)
            try:
                result = reconcile(SessionLocal, provider, env.id, settings)
                logger.info('Workspace %s state=%s', env.id, result)
            except Exception as error:
                # Provider payloads, credentials and user file paths must not be logged.
                logger.warning('Workspace %s needs inspection (%s)', env.id, type(error).__name__)
                with SessionLocal.begin() as db:
                    service = db.get(WorkspaceFileService, env.id)
                    if service:
                        service.status = 'requires_review'
            finally:
                provider.close()
        time.sleep(5)


if __name__ == '__main__':
    main()
