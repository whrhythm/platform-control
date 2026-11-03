"""Attributed logical usage, with explicit unknowns instead of invented zeroes."""
from datetime import timezone
from urllib.parse import quote

from sqlalchemy import select

from .models import (StorageMeasurement, UserEnvironment, UserRegistry, UserObjectStorage,
                     WorkspaceVolumeBinding, now)


class MeasurementError(RuntimeError):
    pass


def sample_harbor(client, binding):
    if binding is None or binding.status != 'ready' or type(binding.project_id) is not int:
        raise MeasurementError('image_registry_not_ready')
    response = client.get('/api/v2.0/projects/' + str(binding.project_id))
    project = response.json() if response.status_code == 200 else {}
    if (project.get('project_id') != binding.project_id or project.get('name') != binding.project_name or
            project.get('metadata', {}).get('public') != 'false'):
        raise MeasurementError('image_project_identity_unverified')
    response = client.get('/api/v2.0/projects/' + str(binding.project_id) + '/summary')
    if response.status_code != 200:
        raise MeasurementError('image_quota_unavailable')
    quota = response.json().get('quota', {})
    used, hard = quota.get('used', {}).get('storage'), quota.get('hard', {}).get('storage')
    if type(used) is not int or not 0 <= used < 2 ** 63 or type(hard) is not int or not -1 <= hard < 2 ** 63:
        raise MeasurementError('image_quota_invalid')
    return {'used_bytes': used, 'budget_bytes': hard if hard >= 0 else None,
            'basis': 'harbor_project_quota', 'scope_id': 'project:' + str(binding.project_id)}


def sample_application_volumes(cluster, env, binding):
    if binding is None or binding.namespace_name != 'codechat-user-' + env.identity_hash[:40]:
        raise MeasurementError('application_volume_binding_unknown')
    response = cluster.client.get(f'api/v1/namespaces/{binding.namespace_name}/persistentvolumeclaims', params={'limit': 100})
    if response.status_code != 200:
        raise MeasurementError('application_volume_inventory_unavailable')
    inventory = response.json()
    if inventory.get('metadata', {}).get('continue') or not isinstance(inventory.get('items'), list):
        raise MeasurementError('application_volume_inventory_incomplete')
    items = inventory['items']
    if len(items) != 1:
        raise MeasurementError('additional_application_volume_unattributed')
    actual = items[0]
    if (actual.get('metadata', {}).get('uid') != binding.pvc_uid or actual.get('metadata', {}).get('name') != 'workspace-data' or
            actual.get('metadata', {}).get('namespace') != binding.namespace_name or
            actual.get('status', {}).get('phase') != 'Bound' or actual.get('spec', {}).get('volumeName') != binding.pv_name):
        raise MeasurementError('application_volume_identity_unverified')
    return {'used_bytes': 0, 'budget_bytes': 0, 'basis': 'no_additional_pvc', 'scope_id': binding.namespace_uid}


def apply_sample(db, env_id, component, sample):
    row = db.scalar(select(StorageMeasurement).where(StorageMeasurement.environment_id == env_id,
                                                    StorageMeasurement.component == component))
    if row is None:
        row = StorageMeasurement(environment_id=env_id, component=component)
        db.add(row)
    if sample is None:
        row.status = 'stale' if row.observed_at else 'unknown'
    else:
        for key in ('used_bytes', 'budget_bytes', 'basis', 'scope_id'):
            setattr(row, key, sample[key])
        row.observed_at, row.status = now(), 'ok'


def reconcile_storage(sessions, cluster, harbor, settings, objects=None):
    if not settings.storage_metering_enabled:
        return
    with sessions() as db:
        environments = list(db.scalars(select(UserEnvironment).where(UserEnvironment.status.not_in(('deleted', 'suspended')))))
    for env in environments:
        with sessions() as db:
            previous = db.scalar(select(StorageMeasurement).where(StorageMeasurement.environment_id == env.id,
                StorageMeasurement.component == 'images'))
            if previous and previous.observed_at and (now() - previous.observed_at.replace(tzinfo=timezone.utc)).total_seconds() < 30:
                continue
            registry, volume, bucket = db.get(UserRegistry, env.id), db.get(WorkspaceVolumeBinding, env.id), db.get(UserObjectStorage, env.id)
        samples = {}
        for component, reader in (
            ('images', lambda: sample_harbor(harbor, registry)),
            ('app_volumes', lambda: sample_application_volumes(cluster, env, volume)),
            ('objects', lambda: objects.sample(bucket, settings.object_storage_scan_limit) if objects and bucket and bucket.status == 'ready' else None),
        ):
            try:
                samples[component] = reader()
            except Exception:
                # Never persist provider exception text or signed requests.
                samples[component] = None
        with sessions.begin() as db:
            db.get(UserEnvironment, env.id, with_for_update=True)
            current = db.get(UserRegistry, env.id)
            if not current or not registry or current.project_id != registry.project_id:
                samples['images'] = None
            current = db.get(UserObjectStorage, env.id)
            if not current or not bucket or current.generation != bucket.generation:
                samples['objects'] = None
            for component, sample in samples.items():
                apply_sample(db, env.id, component, sample)
