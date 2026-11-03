"""Real HTTP metering adapters against local Postman members; synthetic data only."""
import os
from types import SimpleNamespace

import httpx

from control_plane.storage_metering import MeasurementError, sample_application_volumes, sample_harbor


base = os.environ['STORAGE_SIM_URL'].rstrip('/')
assert base.startswith('http://127.0.0.1:')
namespace = 'codechat-user-' + 'a' * 40
collection = f'api/v1/namespaces/{namespace}/persistentvolumeclaims'
pvc = {'metadata': {'name': 'workspace-data', 'namespace': namespace, 'uid': 'synthetic-pvc'},
       'spec': {'volumeName': 'synthetic-pv'}, 'status': {'phase': 'Bound'}}
project = {'project_id': 7, 'name': 'u-synthetic', 'metadata': {'public': 'false'},
           'quota': {'hard': {'storage': -1}, 'used': {'storage': 654321}}}
with httpx.Client(base_url=base + '/harbor/', timeout=2, trust_env=False) as harbor, \
     httpx.Client(base_url=base + '/kubernetes/', timeout=2, trust_env=False) as kube:
    harbor.post('__test__/seed', json={'projects': {project['name']: project}}).raise_for_status()
    kube.post('__test__/seed', json={'objects': {collection: {'metadata': {}, 'items': [pvc]}}}).raise_for_status()
    registry = SimpleNamespace(status='ready', project_id=7, project_name=project['name'])
    binding = SimpleNamespace(namespace_name=namespace, namespace_uid='synthetic-ns', pvc_uid='synthetic-pvc', pv_name='synthetic-pv')
    env = SimpleNamespace(identity_hash='a' * 40)
    assert sample_harbor(harbor, registry) == {'used_bytes': 654321, 'budget_bytes': None,
        'basis': 'harbor_project_quota', 'scope_id': 'project:7'}
    assert sample_application_volumes(SimpleNamespace(client=kube), env, binding)['used_bytes'] == 0
    harbor.post('__test__/seed', json={'projects': {project['name']: project}, 'quota_status': 503}).raise_for_status()
    kube.post('__test__/seed', json={'objects': {collection: {'__status': 503}}}).raise_for_status()
    for call in (lambda: sample_harbor(harbor, registry),
                 lambda: sample_application_volumes(SimpleNamespace(client=kube), env, binding)):
        try:
            call()
        except MeasurementError:
            pass
        else:
            raise AssertionError('An unavailable provider was reported as a known zero')
print('PASS: real HTTP native quota, unlimited budget, verified PVC inventory and both provider outages')
