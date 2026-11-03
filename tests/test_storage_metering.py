from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from control_plane.models import StorageMeasurement, UserEnvironment, now
from control_plane.storage_metering import MeasurementError, apply_sample, sample_harbor, sample_application_volumes
from control_plane.settings import get_settings
from test_capacity import capacity_harness


def harbor_client(project=None, quota=None, status=200):
    def respond(request):
        if request.url.path.endswith('/summary'):
            return httpx.Response(status, json={'quota': quota or {'used': {'storage': 66}, 'hard': {'storage': -1}}})
        return httpx.Response(200, json=project or {'project_id': 3, 'name': 'u-owned', 'metadata': {'public': 'false'}})
    return httpx.Client(transport=httpx.MockTransport(respond), base_url='https://harbor.test')


def test_harbor_uses_native_counter_not_artifact_sizes_and_preserves_unlimited():
    binding = SimpleNamespace(status='ready', project_id=3, project_name='u-owned')
    with harbor_client() as client:
        value = sample_harbor(client, binding)
    assert value == {'used_bytes': 66, 'budget_bytes': None, 'basis': 'harbor_project_quota', 'scope_id': 'project:3'}
    for project, quota in [({'project_id': 4, 'name': 'u-owned', 'metadata': {'public': 'false'}}, None),
        ({'project_id': 3, 'name': 'u-owned', 'metadata': {'public': 'true'}}, None),
        (None, {'used': {'storage': True}, 'hard': {'storage': 500}})]:
        with harbor_client(project, quota) as client, pytest.raises(MeasurementError):
            sample_harbor(client, binding)


def test_extra_application_pvc_zero_requires_complete_exact_inventory():
    env = SimpleNamespace(identity_hash='a' * 64)
    binding = SimpleNamespace(namespace_name='codechat-user-' + 'a' * 40, namespace_uid='ns-uid', pvc_uid='pvc-uid', pv_name='pv-name')
    actual = {'metadata': {'name': 'workspace-data', 'uid': 'pvc-uid', 'namespace': binding.namespace_name},
              'status': {'phase': 'Bound'}, 'spec': {'volumeName': 'pv-name'}}
    def read(inventory):
        client = httpx.Client(base_url='https://cluster.test', transport=httpx.MockTransport(lambda request: httpx.Response(200, json=inventory)))
        with client:
            return sample_application_volumes(SimpleNamespace(client=client), env, binding)
    assert read({'items': [actual]})['used_bytes'] == 0
    for inventory in ({'items': []}, {'items': [actual, actual]}, {'items': [actual], 'metadata': {'continue': 'more'}},
                      {'items': [{**actual, 'metadata': {**actual['metadata'], 'uid': 'replacement'}}]}):
        with pytest.raises(MeasurementError):
            read(inventory)


def test_logical_totals_complete_unknown_and_stale_are_distinct(capacity_harness, monkeypatch):
    client, auth, env_id, *_ = capacity_harness
    monkeypatch.setenv('PLATFORM_STORAGE_METERING_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_PROJECT_DATABASES_ENABLED', 'true')
    get_settings.cache_clear()
    with client.session_factory.begin() as db:
        for kind, used, budget, basis in [('images', 66, None, 'harbor_project_quota'),
            ('objects', 17, 256 * 1024 ** 2, 'completed_object_versions'), ('app_volumes', 0, 0, 'no_additional_pvc')]:
            apply_sample(db, env_id, kind, {'used_bytes': used, 'budget_bytes': budget, 'basis': basis, 'scope_id': 'owned'})
    value = client.get('/internal/v1/workspace/capacity', headers=auth).json()['storage']
    assert value['known_used_bytes'] == 12 + 66 + 17 and value['complete'] is True
    assert value['budget_complete'] is False and value['scope'] == 'managed_logical_storage'
    with client.session_factory.begin() as db:
        row = db.scalar(select(StorageMeasurement).where(StorageMeasurement.environment_id == env_id, StorageMeasurement.component == 'images'))
        row.observed_at = now() - timedelta(minutes=10)
    value = client.get('/internal/v1/workspace/capacity', headers=auth).json()['storage']
    assert value['known_used_bytes'] == 12 + 17 and value['complete'] is False
    assert value['components'][3]['status'] == 'stale' and value['components'][3]['used_bytes'] == 66
    with client.session_factory.begin() as db:
        apply_sample(db, env_id, 'images', None)
    assert client.get('/internal/v1/workspace/capacity', headers=auth).json()['storage']['components'][3]['used_bytes'] == 66
