import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from control_plane.application_controller import Cluster, DeliveryError, cleanup_failure
from control_plane.application_runtime import application_objects, object_path
from control_plane.settings import Settings


@pytest.mark.parametrize('provider', ['local', 'postman'])
@pytest.mark.parametrize('previous', [False, True])
def test_failure_reclaims_only_recorded_compute_and_restores_last_good_release(provider, previous):
    simulation = os.environ.get('CODECHAT_DELIVERY_SIM_URL') if provider == 'postman' else None
    if provider == 'postman' and not simulation:
        pytest.skip('requires local Postman delivery simulation')
    app = SimpleNamespace(id=str(uuid4()), image='harbor/app@sha256:' + 'a' * 64, revision=str(uuid4()),
        desired_state='running', resources={})
    env = SimpleNamespace(identity_hash='b' * 64)
    settings = Settings()
    deployment = application_objects(app, env, settings)[-1]
    deployment['metadata'].update(uid='deployment-uid', resourceVersion='1', generation=1)
    deployment['status'] = {'observedGeneration': 1, 'replicas': 1, 'availableReplicas': 0}
    deployment_path = object_path(deployment['metadata']['namespace'], 'Deployment', deployment['metadata']['name'])
    job_name = 'build-' + uuid4().hex[:24]
    job_path = object_path('codechat-builds', 'Job', job_name)
    job = {'metadata': {'uid': 'job-uid', 'name': job_name, 'labels': {'codechat.io/app-id': app.id}}}
    objects = {job_path: job, deployment_path: deployment}
    mutations = []
    app.resources = {'job': {'name': job_name, 'uid': 'job-uid'}, 'workloads': {'Deployment': {'uid': 'deployment-uid'}}}
    if previous:
        app.resources['last_good'] = {'image': 'harbor/app@sha256:' + 'c' * 64,
                                     'revision': str(uuid4()), 'url': 'http://previous.test/'}

    def provider_http(request):
        path = request.url.path.lstrip('/')
        if request.method == 'GET':
            return httpx.Response(200 if path in objects else 404, json=objects.get(path, {}))
        if request.method == 'DELETE':
            body = json.loads(request.content)
            assert path == job_path
            assert body['preconditions']['uid'] == objects[path]['metadata']['uid']
            assert body['propagationPolicy'] == 'Foreground'
            mutations.append(('DELETE', path))
            objects.pop(path)
            return httpx.Response(202, json={})
        if request.method == 'PATCH':
            assert path == deployment_path
            body = json.loads(request.content)
            assert body[:2] == [{'op': 'test', 'path': '/metadata/uid', 'value': 'deployment-uid'},
                               {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '1'}]
            for item in body[2:]:
                assert item['op'] == 'replace' and item['path'] in ('/spec/template', '/spec/replicas')
                objects[path]['spec'][item['path'].split('/')[-1]] = item['value']
            objects[path]['status'] = {'observedGeneration': 1, 'replicas': objects[path]['spec']['replicas'],
                                       'availableReplicas': objects[path]['spec']['replicas'],
                                       'updatedReplicas': objects[path]['spec']['replicas'], 'readyReplicas': objects[path]['spec']['replicas']}
            mutations.append(('PATCH', path))
            return httpx.Response(200, json=objects[path])
        return httpx.Response(501)

    options = {'base_url': simulation, 'trust_env': False} if simulation else {
        'base_url': 'http://cluster/', 'transport': httpx.MockTransport(provider_http)}
    with httpx.Client(**options) as client:
        if simulation:
            assert client.post('__test__/seed', json={'objects': objects}).status_code == 200
        cluster = Cluster(client)
        for _ in range(5):
            app.resources = cleanup_failure(app, env, cluster, settings)
            if app.resources['cleanup']['status'] == 'complete':
                break
        assert app.resources['cleanup']['status'] == 'complete'
        assert app.resources['cleanup']['runtime'] == ('previous_release' if previous else 'stopped')
        if simulation:
            observed = client.get('__test__/snapshot').json()
            objects, mutations = observed['objects'], observed['mutations']
    assert job_path not in objects
    assert objects[deployment_path]['spec']['replicas'] == (1 if previous else 0)
    if previous:
        assert objects[deployment_path]['spec']['template']['spec']['containers'][0]['image'] == app.resources['last_good']['image']
    assert all('persistentvolume' not in str(item) and 'secrets' not in str(item) for item in mutations)
    assert len(mutations) == 2


def test_cleanup_refuses_an_object_replaced_under_the_same_name():
    app = SimpleNamespace(id=str(uuid4()), resources={'job': {'name': 'build-same', 'uid': 'original'}},
                          image=None, revision=str(uuid4()))
    calls = []
    def provider(request):
        calls.append(request.method)
        return httpx.Response(200, json={'metadata': {'uid': 'replacement', 'labels': {'codechat.io/app-id': app.id}}})
    with httpx.Client(base_url='http://cluster/', transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(DeliveryError, match='cleanup_identity'):
            cleanup_failure(app, SimpleNamespace(identity_hash='b' * 64), Cluster(client), Settings())
    assert calls == ['GET']


def test_late_observation_cannot_overwrite_a_new_release_or_user_stop(tmp_path):
    from control_plane.application_controller import change_app, RevisionChanged
    from control_plane.database import make_engine
    from control_plane.models import Base, Tenant, UserEnvironment, UserApplication
    from sqlalchemy.orm import sessionmaker
    from control_plane.user_environments import identity_digest
    engine = make_engine('sqlite:///' + str(tmp_path / 'revision.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        tenant = Tenant(name='personal', owner_issuer='issuer', owner_subject='owner', project_limit=3)
        db.add(tenant); db.flush()
        env = UserEnvironment(issuer='issuer', subject='owner', identity_hash=identity_digest('issuer', 'owner'),
                              storage_gib=2, first_tenant_id=tenant.id)
        db.add(env); db.flush()
        app = UserApplication(environment_id=env.id, project_path='demo', status='queued', desired_state='stopped')
        db.add(app); db.flush()
        identifier, revision = app.id, app.revision
    with pytest.raises(RevisionChanged):
        change_app(sessions, identifier, expected_revision=str(uuid4()), status='running')
    with pytest.raises(RevisionChanged):
        change_app(sessions, identifier, expected_revision=revision, expected_desired='running', status='running')
    with sessions() as db:
        current = db.get(UserApplication, identifier)
        assert current.status == 'queued' and current.desired_state == 'stopped'
    engine.dispose()


def test_old_ready_pod_cannot_make_a_failed_new_release_look_successful():
    from control_plane.application_controller import deployment_ready
    old_serving = {'metadata': {'generation': 2}, 'status': {'observedGeneration': 2,
        'replicas': 2, 'updatedReplicas': 1, 'readyReplicas': 1, 'availableReplicas': 1}}
    assert not deployment_ready(old_serving)
    old_serving['status']['replicas'] = 1
    assert deployment_ready(old_serving)
    old_serving['status']['observedGeneration'] = 1
    assert not deployment_ready(old_serving)


@pytest.mark.parametrize('mode,expected', [('space', 'build_storage_exhausted'), ('memory', 'build_memory_exhausted'),
    ('deadline', 'build_timed_out'), ('unowned', 'build_failed')])
def test_failed_build_classifies_owned_bounded_diagnostics(mode, expected):
    from control_plane.application_controller import failed_build_code
    from types import SimpleNamespace
    job = {'metadata': {'name': 'build-test', 'uid': 'owned-job'}, 'status': {'conditions': []}}
    pod = {'metadata': {'name': 'build-pod', 'labels': {'codechat.io/app-id': 'app-id'},
        'ownerReferences': [{'kind': 'Job', 'uid': 'foreign-job' if mode == 'unowned' else 'owned-job'}]},
        'status': {'containerStatuses': [{'state': {'terminated': {'reason': 'OOMKilled' if mode == 'memory' else 'Error'}}}]}}
    if mode == 'deadline':
        job['status']['conditions'] = [{'reason': 'DeadlineExceeded'}]
    requests = []
    def logs(path, params):
        requests.append(params)
        return httpx.Response(200, text='no space left on device')
    cluster = SimpleNamespace(get=lambda path: {'items': [pod]}, client=SimpleNamespace(get=logs))
    assert failed_build_code(cluster, SimpleNamespace(id='app-id'), job) == expected
    assert len(requests) == (1 if mode == 'space' else 0)
