"""Observed build/push/deploy reconciliation. No user source or credentials in logs."""
from copy import deepcopy
from types import SimpleNamespace
import base64
import json
import logging
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
import httpx
from sqlalchemy import select

from .application_runtime import application_objects, application_origin, app_name, build_objects, object_path
from .application_observation import observe
from .database import SessionLocal
from .project_database_controller import reconcile_databases
from .models import UserApplication, UserEnvironment, UserRegistry, WorkspaceFileService, WorkspaceVolumeBinding
from .registry import provision_registry, RegistryError
from .settings import get_settings

logger = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    pass


class RevisionChanged(RuntimeError):
    pass


def deployment_ready(actual):
    status = actual.get('status', {})
    return (status.get('observedGeneration', 0) >= actual['metadata'].get('generation', 1) and
            all(status.get(field, 0) == 1 for field in ('replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas')))


def failed_build_code(cluster, app, job):
    """Classify a bounded owned build log; never persist source or log contents."""
    if any(value.get('reason') == 'DeadlineExceeded' for value in job.get('status', {}).get('conditions', [])):
        return 'build_timed_out'
    try:
        pods = cluster.get('api/v1/namespaces/codechat-builds/pods?labelSelector=job-name%3D' + job['metadata']['name'])
        for pod in (pods or {}).get('items', []):
            metadata = pod.get('metadata', {})
            if (metadata.get('labels', {}).get('codechat.io/app-id') != app.id or not any(
                    value.get('kind') == 'Job' and value.get('uid') == job['metadata']['uid']
                    for value in metadata.get('ownerReferences', []))):
                continue
            statuses = pod.get('status', {}).get('containerStatuses', [])
            if any(value.get('state', {}).get('terminated', {}).get('reason') == 'OOMKilled' for value in statuses):
                return 'build_memory_exhausted'
            response = cluster.client.get('api/v1/namespaces/codechat-builds/pods/' + metadata['name'] + '/log',
                params={'container': 'build', 'tailLines': 80, 'limitBytes': 16384})
            if response.status_code == 200 and 'no space left on device' in response.text.lower():
                return 'build_storage_exhausted'
    except Exception:
        # Diagnostics must not prevent cleanup when a Pod has already disappeared.
        pass
    return 'build_failed'


class Cluster:
    def __init__(self, client):
        self.client = client

    def get(self, path):
        response = self.client.get(path)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise DeliveryError('provider_read_failed')
        return response.json()

    def create(self, document):
        response = self.client.post(object_path(document['metadata']['namespace'], document['kind']), json=document)
        if response.status_code != 201:
            raise DeliveryError('provider_create_requires_review')
        return response.json()

    def patch(self, path, actual, operations):
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': actual['metadata']['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': actual['metadata']['resourceVersion']}] + operations
        response = self.client.patch(path, content=json.dumps(patch), headers={'Content-Type': 'application/json-patch+json'})
        if response.status_code != 200:
            raise DeliveryError('provider_update_requires_review')

    def delete(self, path, uid, *, foreground=False):
        response = self.client.request('DELETE', path, json={'apiVersion': 'v1', 'kind': 'DeleteOptions',
            'preconditions': {'uid': uid}, 'propagationPolicy': 'Foreground' if foreground else 'Background'})
        if response.status_code not in (200, 202, 404):
            raise DeliveryError('provider_delete_requires_review')


def check_owned(actual, desired, uid=None):
    meta, expected = actual.get('metadata', {}), desired['metadata']
    if (meta.get('name') != expected['name'] or meta.get('namespace') != expected['namespace'] or
            (uid and meta.get('uid') != uid) or not meta.get('uid') or
            any(meta.get('labels', {}).get(key) != value for key, value in expected['labels'].items())):
        raise DeliveryError('provider_identity_requires_review')
    return meta['uid']


def change_app(sessions, identifier, *, expected_revision=None, expected_desired=None, **fields):
    with sessions.begin() as db:
        app = db.scalar(select(UserApplication).where(UserApplication.id == identifier).with_for_update())
        if ((expected_revision is not None and app.revision != expected_revision) or
                (expected_desired is not None and app.desired_state != expected_desired)):
            raise RevisionChanged()
        for key, value in fields.items():
            setattr(app, key, value)


def cleanup_failure(app, env, cluster, settings):
    """Reclaim only recorded compute; keep files and any last successful release."""
    state = deepcopy(app.resources)
    state['cleanup'] = {'status': 'pending'}
    job = state.get('job', {})
    if job.get('uid') and job.get('name'):
        path = object_path('codechat-builds', 'Job', job['name'])
        actual = cluster.get(path)
        if actual:
            meta = actual.get('metadata', {})
            if (meta.get('uid') != job['uid'] or meta.get('labels', {}).get('codechat.io/app-id') != app.id):
                raise DeliveryError('cleanup_identity_requires_review')
            if not meta.get('deletionTimestamp'):
                cluster.delete(path, job['uid'], foreground=True)
            return state
    step = state.get('workloads', {}).get('Deployment', {})
    if step.get('uid'):
        good = state.get('last_good', {})
        fallback = bool(good.get('image') and good.get('revision'))
        target = SimpleNamespace(id=app.id, image=good['image'] if fallback else app.image,
                                 revision=good['revision'] if fallback else app.revision,
                                 desired_state='running' if fallback else 'stopped')
        desired = application_objects(target, env, settings, database_secret=good.get('database_secret') if fallback else None)[-1]
        path = object_path(desired['metadata']['namespace'], 'Deployment', desired['metadata']['name'])
        actual = cluster.get(path)
        if actual:
            check_owned(actual, desired, step['uid'])
            operations = []
            if actual['spec'].get('replicas') != desired['spec']['replicas']:
                operations.append({'op': 'replace', 'path': '/spec/replicas', 'value': desired['spec']['replicas']})
            if fallback and actual['spec']['template']['metadata'].get('annotations', {}).get('codechat.io/revision') != good['revision']:
                operations.append({'op': 'replace', 'path': '/spec/template', 'value': desired['spec']['template']})
            if operations:
                cluster.patch(path, actual, operations)
                return state
            observed = actual.get('status', {})
            if (observed.get('observedGeneration', 0) < actual['metadata'].get('generation', 1) or
                    (not deployment_ready(actual) if fallback else observed.get('replicas', 0) != 0)):
                return state
        state['cleanup']['runtime'] = 'previous_release' if fallback else 'stopped'
    state['cleanup']['status'] = 'complete'
    return state


def provision_pull_secret(sessions, env, registry, cluster, settings, cipher):
    if registry.pull_secret_uid:
        return
    auth = base64.b64encode((registry.robot_name + ':' + cipher.decrypt(registry.encrypted_secret.encode()).decode()).encode()).decode()
    host = urlsplit(settings.harbor_url).netloc
    document = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'kubernetes.io/dockerconfigjson',
        'metadata': {'name': 'application-registry', 'namespace': 'codechat-user-' + env.identity_hash[:40],
                     'labels': {'app.kubernetes.io/managed-by': 'platform-control', 'codechat.io/user-hash': env.identity_hash[:40]}},
        'stringData': {'.dockerconfigjson': json.dumps({'auths': {host: {'auth': auth}}})}}
    with sessions.begin() as db:
        row = db.get(UserRegistry, env.id)
        if row.status != 'ready' or row.pull_secret_uid:
            return
        row.status = 'pullsecret_inflight'
    actual = cluster.create(document)
    uid = check_owned(actual, document)
    with sessions.begin() as db:
        row = db.get(UserRegistry, env.id)
        row.pull_secret_uid, row.status = uid, 'ready'


def reconcile_application(sessions, app_id, cluster, harbor, settings):
    cipher = Fernet(settings.model_credential_encryption_key.encode())
    with sessions() as db:
        app = db.get(UserApplication, app_id)
        env = db.get(UserEnvironment, app.environment_id)
        registry = db.get(UserRegistry, env.id)
        binding, files = db.get(WorkspaceVolumeBinding, env.id), db.get(WorkspaceFileService, env.id)
        if app.status != 'failed' and registry and registry.status == 'requires_review':
            raise DeliveryError('registry_requires_review')
        if app.status != 'failed' and (not registry or registry.status != 'ready' or not registry.pull_secret_uid or
                not binding or binding.namespace_name != 'codechat-user-' + env.identity_hash[:40] or
                not files or files.status != 'ready' or env.status in ('deleted', 'suspended')):
            return
        if app.status == 'failed' and (not binding or binding.namespace_name != 'codechat-user-' + env.identity_hash[:40]):
            return
    state = deepcopy(app.resources)
    def update(**fields):
        fields.setdefault('expected_revision', app.revision)
        fields.setdefault('expected_desired', app.desired_state)
        return change_app(sessions, app_id, **fields)
    if app.status == 'failed':
        if state.get('cleanup', {}).get('status') == 'complete':
            return
        try:
            cleaned = cleanup_failure(app, env, cluster, settings)
        except (DeliveryError, httpx.HTTPError):
            cleaned = deepcopy(state)
            cleaned['cleanup'] = {'status': 'pending', 'error_code': 'cleanup_requires_attention'}
        restored = cleaned.get('cleanup', {}).get('runtime') == 'previous_release' and cleaned.get('cleanup', {}).get('status') == 'complete'
        update(resources=cleaned, **({'image': cleaned['last_good']['image']} if restored else {}))
        return
    if app.status == 'queued':
        update(status='preparing')
        app.status = 'preparing'
    if app.status == 'preparing':
        if any(value.get('state') == 'inflight' for value in (state.get('job', {}), state.get('secret', {}))):
            raise DeliveryError('build_write_requires_review')
        job, secret, target = build_objects(app, env, registry, settings,
            cipher.decrypt(registry.encrypted_secret.encode()).decode(),
            cipher.decrypt(files.encrypted_token.encode()).decode(), Path(settings.workspace_service_ca).read_text())
        if not state.get('job'):
            state['job'] = {'state': 'inflight'}
            update(resources=deepcopy(state))
            actual = cluster.create(job)
            state['job'] = {'state': 'created', 'uid': check_owned(actual, job), 'name': job['metadata']['name']}
            update(resources=deepcopy(state))
        if not state.get('secret'):
            secret['metadata']['ownerReferences'] = [{'apiVersion': 'batch/v1', 'kind': 'Job',
                'name': state['job']['name'], 'uid': state['job']['uid']}]
            state['secret'] = {'state': 'inflight'}
            update(resources=deepcopy(state))
            actual = cluster.create(secret)
            state['secret'] = {'state': 'created', 'uid': check_owned(actual, secret)}
            update(resources=deepcopy(state), status='building')
        return
    if app.status == 'building':
        job = state.get('job', {})
        actual = cluster.get(object_path('codechat-builds', 'Job', job.get('name', '')))
        if not actual or actual.get('metadata', {}).get('uid') != job.get('uid'):
            raise DeliveryError('build_identity_requires_review')
        if any(item['type'] == 'Failed' and item['status'] == 'True' for item in actual.get('status', {}).get('conditions', [])):
            raise DeliveryError(failed_build_code(cluster, app, actual))
        if not any(item['type'] == 'Complete' and item['status'] == 'True' for item in actual.get('status', {}).get('conditions', [])):
            return
        response = harbor.get(f'/api/v2.0/projects/{registry.project_name}/repositories/{app_name(app)}/artifacts/{app.revision.replace("-", "")}')
        digest = response.json().get('digest', '') if response.status_code == 200 else ''
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise DeliveryError('pushed_image_verify_failed')
        image = f'{urlsplit(settings.harbor_url).netloc}/{registry.project_name}/{app_name(app)}@{digest}'
        update(image=image, status='deploying')
        return
    if app.status in ('deploying', 'running', 'stopped', 'failed', 'changing'):
        objects = application_objects(app, env, settings, database_secret=state.get('database_secret'))
        workloads = deepcopy(state.get('workloads', {}))
        for desired in objects:
            kind, meta = desired['kind'], desired['metadata']
            path = object_path(meta['namespace'], kind, meta['name'])
            step = workloads.get(kind)
            if step and step.get('state') == 'inflight':
                raise DeliveryError('application_write_requires_review')
            if app.desired_state == 'deleted':
                if step and step.get('state') == 'created':
                    cluster.delete(path, step['uid'])
                    workloads[kind] = {'state': 'deleting', 'uid': step['uid']}
                    state['workloads'] = deepcopy(workloads)
                    update(resources=deepcopy(state))
                elif step and step.get('state') == 'deleting':
                    actual = cluster.get(path)
                    if actual:
                        check_owned(actual, desired, step['uid'])
                        return
                    workloads[kind] = {'state': 'deleted'}
                    state['workloads'] = deepcopy(workloads)
                    update(resources=deepcopy(state))
                continue
            if not app.image or app.status == 'failed' and app.desired_state == 'running':
                return
            if not step or step.get('state') == 'deleted':
                workloads[kind] = {'state': 'inflight'}
                state['workloads'] = deepcopy(workloads)
                update(resources=deepcopy(state))
                actual = cluster.create(desired)
                workloads[kind] = {'state': 'created', 'uid': check_owned(actual, desired)}
                state['workloads'] = deepcopy(workloads)
                update(resources=deepcopy(state))
            else:
                actual = cluster.get(path)
                if not actual:
                    raise DeliveryError('application_missing_requires_review')
                check_owned(actual, desired, step['uid'])
                if kind == 'ApisixRoute' and actual['spec'] != desired['spec']:
                    cluster.patch(path, actual, [{'op': 'replace', 'path': '/spec', 'value': desired['spec']}])
                    if app.desired_state == 'running':
                        update(status='deploying', url=None)
                    return
                if kind == 'Deployment':
                    operations = []
                    if actual['spec'].get('replicas') != desired['spec']['replicas']:
                        operations.append({'op': 'replace', 'path': '/spec/replicas', 'value': desired['spec']['replicas']})
                    if actual['spec']['template']['metadata'].get('annotations', {}).get('codechat.io/revision') != app.revision:
                        operations.append({'op': 'replace', 'path': '/spec/template', 'value': desired['spec']['template']})
                    if operations:
                        cluster.patch(path, actual, operations)
                        return
        if app.desired_state == 'deleted':
            if all(step.get('state') == 'deleted' for step in workloads.values()):
                update(status='deleted', url=None, observation=None)
            return
        if app.desired_state == 'stopped':
            desired = objects[-1]
            actual = cluster.get(object_path(desired['metadata']['namespace'], 'Deployment', desired['metadata']['name']))
            if actual and not actual.get('status', {}).get('replicas', 0):
                update(status='stopped', observation=None)
            return
        deployment = objects[-1]
        namespace, name = deployment['metadata']['namespace'], deployment['metadata']['name']
        actual = cluster.get(object_path(namespace, 'Deployment', name))
        # A previous failed revision can retain its condition while Kubernetes
        # has not yet observed the replacement. Do not fail the new rollout.
        if not actual or actual.get('status', {}).get('observedGeneration', 0) < actual['metadata'].get('generation', 1):
            return
        if any(condition.get('status') == 'False' and condition.get('reason') == 'ProgressDeadlineExceeded'
                          for condition in actual.get('status', {}).get('conditions', [])):
            raise DeliveryError('application_start_failed')
        if not deployment_ready(actual):
            return
        route = cluster.get(object_path(namespace, 'ApisixRoute', name))
        if not route or not any(item.get('type') == 'Accepted' and item.get('status') == 'True' and
                                item.get('observedGeneration', 0) >= route['metadata'].get('generation', 1)
                                for item in route.get('status', {}).get('conditions', [])):
            return
        try:
            with httpx.Client(timeout=5, trust_env=False) as client:
                response = client.get(f'http://{name}.{namespace}.svc:8080/')
            if not 200 <= response.status_code < 400:
                return
        except httpx.HTTPError:
            return
        try:
            observation = observe(cluster, namespace, app.id)
        except (httpx.HTTPError, ValueError, KeyError):
            observation = None
        state['last_good'] = {'image': app.image, 'revision': app.revision, 'url': application_origin(app, settings) + '/',
                              'database_secret': state.get('database_secret')}
        update(expected_revision=app.revision, expected_desired=app.desired_state,
                   status='running', error_code=None, observation=observation, resources=state,
                   url=application_origin(app, settings) + '/')


def main():
    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    if not settings.publisher_enabled or not settings.buildkit_image or not settings.harbor_password:
        raise RuntimeError('Application delivery is not configured')
    service_account = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    kube_context = ssl.create_default_context(cafile=str(service_account / 'ca.crt'))
    registry_context = ssl.create_default_context(cafile=settings.workspace_service_ca)
    while True:
        with httpx.Client(base_url='https://kubernetes.default.svc/', verify=kube_context, timeout=15,
                          headers={'Authorization': 'Bearer ' + (service_account / 'token').read_text().strip()}, trust_env=False) as client, \
             httpx.Client(base_url=settings.harbor_url, verify=registry_context, timeout=15,
                          auth=(settings.harbor_username, settings.harbor_password), trust_env=False) as harbor:
            cluster = Cluster(client)
            reconcile_databases(SessionLocal, cluster, settings)
            with SessionLocal() as db:
                environments = list(db.scalars(select(UserEnvironment).where(UserEnvironment.status.not_in(('deleted', 'suspended')))))
            for env in environments:
                try:
                    with SessionLocal() as db:
                        previous = db.get(UserRegistry, env.id)
                        if previous and previous.status == 'requires_review':
                            continue
                        files = db.get(WorkspaceFileService, env.id)
                        if not files or files.status != 'ready' or files.resources.get('RoleBinding', {}).get('state') != 'created':
                            continue
                    provision_registry(SessionLocal, env.id, harbor, settings.model_credential_encryption_key)
                    with SessionLocal() as db:
                        registry = db.get(UserRegistry, env.id)
                    provision_pull_secret(SessionLocal, env, registry, cluster, settings,
                                          Fernet(settings.model_credential_encryption_key.encode()))
                except Exception as error:
                    with SessionLocal.begin() as db:
                        registry = db.get(UserRegistry, env.id)
                        if registry:
                            registry.status = 'requires_review'
                    code = str(error) if isinstance(error, RegistryError) else type(error).__name__
                    logger.warning('Registry environment=%s requires inspection (%s)', env.id, code)
            with SessionLocal() as db:
                identifiers = list(db.scalars(select(UserApplication.id).where(UserApplication.status != 'deleted')))
            for identifier in identifiers:
                with SessionLocal() as db:
                    requested = db.get(UserApplication, identifier)
                    expected_revision, expected_desired = requested.revision, requested.desired_state
                try:
                    reconcile_application(SessionLocal, identifier, cluster, harbor, settings)
                except RevisionChanged:
                    continue
                except Exception as error:
                    code = str(error) if isinstance(error, DeliveryError) else 'delivery_requires_review'
                    try:
                        change_app(SessionLocal, identifier, expected_revision=expected_revision, expected_desired=expected_desired,
                                   status='failed', error_code=code)
                    except RevisionChanged:
                        continue
                    logger.warning('Application=%s state=%s (%s)', identifier, code, type(error).__name__)
        time.sleep(5)


if __name__ == '__main__':
    main()
