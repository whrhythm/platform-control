from types import SimpleNamespace

import httpx
import pytest

from control_plane.application_observation import observe
from control_plane.application_resources import CPU, MEMORY, plan_resources, resources_match, container_resources
from control_plane.capacity import CapacityPlan


@pytest.mark.parametrize('plan,cpu,memory', [('existing', 250, 256), ('basic', 500, 512), ('pro', 1000, 1024), ('unknown', 250, 256)])
def test_legacy_snapshot_defaults_and_explicit_entitlements(plan, cpu, memory):
    assert plan_resources({'id': plan}) == {CPU: cpu, MEMORY: memory}
    assert plan_resources({'id': plan, CPU: 750, MEMORY: 768}) == {CPU: 750, MEMORY: 768}


@pytest.mark.parametrize('invalid', [0, -1, True, '500', 4001])
def test_invalid_cpu_cannot_silently_fall_back(invalid):
    with pytest.raises(ValueError):
        plan_resources({'id': 'basic', CPU: invalid})
    with pytest.raises(ValueError):
        CapacityPlan(id='basic', version='v1', name='basic', storage_gib=4, application_limit=5,
                     amount_minor=1000, currency='CNY', application_cpu_millicores=invalid)


def test_normalized_resource_equality_does_not_roll_forever():
    actual = {'requests': {'cpu': '0.1', 'memory': '134217728'}, 'limits': {'cpu': '1', 'memory': '1Gi'}}
    desired = container_resources(plan_resources({'id': 'pro'}))
    assert resources_match(actual, desired)
    assert not resources_match(actual, container_resources(plan_resources({'id': 'basic'})))


@pytest.mark.parametrize('limits', [True, False])
def test_metrics_use_actual_app_limits_and_ignore_sidecars_and_terminating_pods(limits):
    pod = {'metadata': {'name': 'current', 'labels': {'codechat.io/app-id': 'app'}, 'creationTimestamp': '2026-01-01'},
           'spec': {'containers': [{'name': 'app', 'resources': container_resources(plan_resources({'id': 'basic'})) if limits else {}}]}}
    def respond(request):
        if request.url.path.endswith('/pods'):
            return httpx.Response(200, json={'items': [pod, {'metadata': {'name': 'terminating', 'labels': {'codechat.io/app-id': 'app'},
                'creationTimestamp': '2027-01-01', 'deletionTimestamp': '2027-01-01'}}]})
        assert 'terminating' not in str(request.url)
        if request.url.path.endswith('/log'):
            return httpx.Response(200, text='hello')
        return httpx.Response(200, json={'containers': [
            {'name': 'app', 'usage': {'cpu': '250m', 'memory': '256Mi'}},
            {'name': 'sidecar', 'usage': {'cpu': '1000m', 'memory': '1Gi'}}]})
    with httpx.Client(base_url='http://cluster/', transport=httpx.MockTransport(respond)) as client:
        result = observe(SimpleNamespace(client=client), 'test', 'app')
    assert result['logs'] == 'hello'
    if not limits:
        assert result['metrics'] is None
    else:
        assert result['metrics']['cpu_limit_millicores'] == 500
        assert result['metrics']['memory_limit_bytes'] == 512 * 1024 ** 2
        assert result['metrics']['cpu_percent'] == result['metrics']['memory_percent'] == 50
