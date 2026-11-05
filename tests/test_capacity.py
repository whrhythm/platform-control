from copy import deepcopy
from datetime import timedelta
import json

import httpx
import pytest
from sqlalchemy import select

from control_plane.capacity import GIB
from control_plane.capacity_controller import reconcile_capacity
from control_plane.kubernetes_workspace_provider import KubernetesWorkspaceProvider
from control_plane.models import (CapacityAccount, CapacityOrder, UserApplication, UserEnvironment,
    WorkspaceFileService, WorkspaceProvisioningIntent, WorkspaceVolumeBinding, now)
from control_plane.settings import get_settings
from control_plane.user_environments import plan_user_environment

PREFIX = '/internal/v1/workspace/capacity'


@pytest.fixture
def capacity_harness(harness, monkeypatch):
    client, _ = harness
    return prepare_capacity(client, monkeypatch)


def prepare_capacity(client, monkeypatch):
    monkeypatch.setenv('PLATFORM_WORKSPACE_FILES_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_TEST_PAYMENTS_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_CAPACITY_UPGRADES_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_LIBRECHAT_SERVICE_TOKEN', 's' * 40)
    get_settings.cache_clear()
    auth = {'X-CodeChat-Service-Token': 's' * 40, 'X-CodeChat-Subject': 'alice'}
    assert client.post('/internal/v1/workspace/ensure', headers=auth).status_code == 200
    with client.session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.subject == 'alice'))
        env_id, issuer = env.id, env.issuer
        namespace = 'codechat-user-' + env.identity_hash[:40]
        intent = db.get(WorkspaceProvisioningIntent, env.id)
        intent.state, intent.namespace_uid, intent.quota_uid, intent.policy_uid, intent.pvc_uid = (
            'pvc_recorded', 'ns-uid', 'quota-uid', 'policy-uid', 'pvc-uid')
        db.add(WorkspaceVolumeBinding(environment_id=env_id, namespace_name=namespace, namespace_uid='ns-uid',
            pvc_uid='pvc-uid', pv_name='pv-test', pv_uid='pv-uid', volume_handle='volume-test'))
        db.add(WorkspaceFileService(environment_id=env_id, encrypted_token='synthetic', status='ready', resources={}))
    observed = {'used_bytes': 12, 'scope': 'personal_volume', 'observed_at': now().isoformat(), 'physical_total_bytes': GIB * 19 // 10}
    def usage(*args, **kwargs):
        return httpx.Response(200, json=observed)
    monkeypatch.setattr('control_plane.capacity.call_files', usage)
    return client, auth, env_id, observed, issuer


def create_order(client, auth, *, key='capacity-key-0001', plan='basic'):
    offers = client.get(PREFIX, headers=auth).json()['plans']
    version = next(item['version'] for item in offers if item['id'] == plan)
    return client.post(PREFIX + '/orders', headers={**auth, 'Idempotency-Key': key},
                       json={'plan_id': plan, 'plan_version': version})


def test_snapshot_idempotency_confirmation_and_cancellation(capacity_harness, monkeypatch):
    client, auth, env_id, _, _ = capacity_harness
    before = client.get(PREFIX, headers=auth).json()
    assert before['plan']['storage_gib'] == 2 and before['applications']['limit'] == 3
    assert before['plan']['application_cpu_millicores'] == 250
    assert before['trial']['application_memory_mib'] == 256
    assert before['storage']['known_used_bytes'] == 12 and before['storage']['complete'] is False
    assert all(item['used_bytes'] is None for item in before['storage']['components'][1:])
    created = create_order(client, auth)
    assert created.status_code == 201
    order = created.json()
    assert order['status'] == 'pending_payment' and order['plan']['amount_minor'] == 1000
    assert order['plan']['application_cpu_millicores'] == 500
    assert order['plan']['application_memory_mib'] == 512
    assert create_order(client, auth).json()['id'] == order['id']
    assert create_order(client, auth, plan='pro').status_code == 409
    assert create_order(client, auth, key='capacity-key-0002').status_code == 409
    assert client.post(PREFIX + '/orders/' + order['id'] + '/confirm-test', headers=auth).json()['status'] == 'applying'
    again = client.post(PREFIX + '/orders/' + order['id'] + '/confirm-test', headers=auth).json()
    assert again['attempt'] == 1 and again['payment_status'] == 'confirmed'
    assert client.get(PREFIX, headers=auth).json()['plan']['storage_gib'] == 2
    assert client.get(PREFIX, headers=auth).json()['plan']['application_cpu_millicores'] == 250
    assert client.post(PREFIX + '/orders/' + order['id'] + '/cancel', headers=auth).status_code == 409
    assert client.post(PREFIX + '/orders/' + order['id'] + '/confirm-test', headers={**auth, 'X-CodeChat-Subject': 'bob'}).status_code == 404
    monkeypatch.setenv('PLATFORM_TEST_PAYMENTS_ENABLED', 'false')
    get_settings.cache_clear()
    assert client.post(PREFIX + '/orders/' + order['id'] + '/confirm-test', headers=auth).status_code == 403


def test_service_caps_do_not_add_to_the_activated_plan_budget(capacity_harness, monkeypatch):
    from control_plane.models import ProjectDatabase, StorageMeasurement
    client, auth, env_id, _, _ = capacity_harness
    client.get(PREFIX, headers=auth)
    with client.session_factory.begin() as db:
        env = db.get(UserEnvironment, env_id)
        env.storage_gib = 4
        account = db.get(CapacityAccount, env_id)
        account.plan = {**account.plan, 'storage_gib': 4}
        db.add(ProjectDatabase(environment_id=env_id, project_path='demo', encrypted_password='synthetic',
            reserved_bytes=256 * 1024 ** 2, used_bytes=8 * 1024 ** 2, status='ready', observed_at=now()))
        for kind, used in [('objects', 1024 ** 2), ('images', 80 * 1024 ** 2)]:
            sample = db.scalar(select(StorageMeasurement).where(
                StorageMeasurement.environment_id == env_id, StorageMeasurement.component == kind))
            sample.budget_bytes = None if kind == 'images' else 256 * 1024 ** 2
            sample.used_bytes, sample.status, sample.observed_at = used, 'ok', now()
    monkeypatch.setenv('PLATFORM_PROJECT_DATABASES_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_PROJECT_DATABASE_BUDGET_MIB', '256')
    get_settings.cache_clear()
    result = client.get(PREFIX, headers=auth).json()
    assert result['storage']['budget_bytes'] == 4 * GIB
    assert result['storage']['budget_complete'] is True
    assert result['storage']['known_used_bytes'] == 89 * 1024 ** 2 + 12
    assert result['plan']['storage_gib'] == 4


def test_unknown_measurement_stale_quote_and_cancelled_order(capacity_harness):
    client, auth, _, observed, _ = capacity_harness
    client.get(PREFIX, headers=auth)
    observed.clear()
    result = client.get(PREFIX, headers=auth).json()
    assert result['storage']['known_used_bytes'] is None
    assert result['storage']['components'][0]['status'] == 'stale'
    assert result['storage']['components'][0]['used_bytes'] == 12
    assert client.post(PREFIX + '/orders', headers={**auth, 'Idempotency-Key': 'stale-version-0001'},
                       json={'plan_id': 'basic', 'plan_version': 'old'}).status_code == 409
    order = create_order(client, auth).json()
    path = PREFIX + '/orders/' + order['id']
    assert client.post(path + '/cancel', headers=auth).json()['status'] == 'cancelled'
    assert client.post(path + '/cancel', headers=auth).json()['status'] == 'cancelled'
    assert client.post(path + '/confirm-test', headers=auth).status_code == 409
    assert create_order(client, auth, key='capacity-new-order').status_code == 201


def test_price_change_invalidates_old_offer_and_confirm_uses_original_snapshot(capacity_harness, monkeypatch):
    client, auth, _, _, _ = capacity_harness
    offer = client.get(PREFIX, headers=auth).json()['plans'][0]
    order = create_order(client, auth).json()
    raw = json.loads(get_settings().capacity_plans_json)
    raw[0]['amount_minor'] += 100
    monkeypatch.setenv('PLATFORM_CAPACITY_PLANS_JSON', json.dumps(raw))
    get_settings.cache_clear()
    current = client.get(PREFIX, headers=auth).json()['plans'][0]
    assert current['version'] != offer['version']
    replay = client.post(PREFIX + '/orders', headers={**auth, 'Idempotency-Key': 'capacity-key-0001'},
                         json={'plan_id': offer['id'], 'plan_version': offer['version']})
    assert replay.json()['id'] == order['id']
    confirmed = client.post(PREFIX + '/orders/' + order['id'] + '/confirm-test', headers=auth).json()
    assert confirmed['plan']['amount_minor'] == offer['amount_minor']


class ResizeHTTP:
    def __init__(self, issuer):
        plan = plan_user_environment(issuer, 'alice', storage_gib=2)
        self.objects = {item['kind']: deepcopy(item) for item in plan['objects']}
        for kind, item in self.objects.items():
            item['metadata'].update(uid={'Namespace': 'ns-uid', 'PersistentVolumeClaim': 'pvc-uid', 'ResourceQuota': 'quota-uid', 'NetworkPolicy': 'policy-uid'}[kind], resourceVersion='1')
        self.objects['Namespace']['status'] = {'phase': 'Active'}
        self.objects['PersistentVolumeClaim']['spec']['volumeName'] = 'pv-test'
        self.objects['PersistentVolumeClaim']['status'] = {'phase': 'Bound', 'capacity': {'storage': '2Gi'}}
        self.objects['PersistentVolume'] = {'metadata': {'name': 'pv-test', 'uid': 'pv-uid'}, 'status': {'phase': 'Bound'},
            'spec': {'capacity': {'storage': '2Gi'}, 'storageClassName': 'codechat-workspace-retain', 'persistentVolumeReclaimPolicy': 'Retain',
                'claimRef': {'namespace': plan['namespace'], 'name': 'workspace-data', 'uid': 'pvc-uid'},
                'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'volume-test'}}}
        self.calls = []
        self.fail_patch_response = False

    def __call__(self, request):
        path = request.url.path
        if 'storageclasses' in path:
            return httpx.Response(200, json={'allowVolumeExpansion': True, 'provisioner': 'driver.longhorn.io'})
        kind = next((kind for term, kind in [('resourcequotas', 'ResourceQuota'), ('persistentvolumeclaims', 'PersistentVolumeClaim'), ('persistentvolumes', 'PersistentVolume')]
                     if term in path), 'Namespace')
        item = self.objects[kind]
        if request.method == 'PATCH':
            patches = json.loads(request.content)
            assert patches[0] == {'op': 'test', 'path': '/metadata/uid', 'value': item['metadata']['uid']}
            assert patches[1]['value'] == item['metadata']['resourceVersion']
            self.calls.append((kind, patches[-1]))
            target = item
            parts = patches[-1]['path'].strip('/').split('/')
            for key in parts[:-1]:
                target = target[key]
            target[parts[-1]] = patches[-1]['value']
            item['metadata']['resourceVersion'] = str(int(item['metadata']['resourceVersion']) + 1)
            if self.fail_patch_response:
                self.fail_patch_response = False
                return httpx.Response(503, json={})
        return httpx.Response(200, json=deepcopy(item))


def provider_for(issuer, transport):
    provider = KubernetesWorkspaceProvider(api_url='http://127.0.0.1:1', token='synthetic', issuer=issuer, subject='alice', storage_gib=2, allow_test_loopback_http=True)
    provider.client.close()
    provider.client = httpx.Client(base_url='http://127.0.0.1:1/', transport=httpx.MockTransport(transport))
    return provider


def test_resize_requires_kubernetes_and_filesystem_observation_and_recovers_lost_reply(capacity_harness):
    client, auth, env_id, observed, issuer = capacity_harness
    order = create_order(client, auth).json()
    client.post(PREFIX + '/orders/' + order['id'] + '/confirm-test', headers=auth)
    transport = ResizeHTTP(issuer)
    provider = provider_for(issuer, transport)
    settings = get_settings()
    try:
        run = lambda: reconcile_capacity(client.session_factory, provider, env_id, settings, probe=lambda: observed)
        run()  # Durable baseline, no provider writes.
        assert transport.calls == []
        transport.fail_patch_response = True
        run()  # Quota changed, but its reply was lost.
        run()  # Observe quota and increase the same PVC.
        run()  # PVC request alone cannot activate rights.
        assert [kind for kind, _ in transport.calls] == ['ResourceQuota', 'PersistentVolumeClaim']
        from control_plane.workspace_controller import reconcile
        disabled = settings.model_copy(update={'capacity_upgrades_enabled': False, 'test_payments_enabled': False})
        assert reconcile(client.session_factory, provider, env_id, disabled) == 'capacity_in_progress'
        with client.session_factory() as db:
            assert db.get(UserEnvironment, env_id).storage_gib == 2
            assert db.get(WorkspaceFileService, env_id).status == 'ready'
        transport.objects['PersistentVolumeClaim']['status']['capacity']['storage'] = '4Gi'
        transport.objects['PersistentVolume']['spec']['capacity']['storage'] = '4Gi'
        run()  # The guest filesystem is still old.
        assert client.get(PREFIX, headers=auth).json()['plan']['storage_gib'] == 2
        observed['physical_total_bytes'] = GIB * 38 // 10
        run()
        result = client.get(PREFIX, headers=auth).json()
        assert result['plan']['storage_gib'] == 4 and result['applications']['limit'] == 5
        assert result['plan']['application_cpu_millicores'] == 500
        assert result['plan']['application_memory_mib'] == 512
        assert result['active_order'] is None
        assert transport.objects['PersistentVolumeClaim']['metadata']['uid'] == 'pvc-uid'
        assert client.post(PREFIX + '/orders/' + order['id'] + '/confirm-test', headers=auth).json()['attempt'] == 1
        assert reconcile_capacity(client.session_factory, provider, env_id, settings, probe=lambda: observed) is False
    finally:
        provider.close()


def test_expansion_failure_keeps_rights_and_explicit_retry_reuses_payment(capacity_harness):
    client, auth, env_id, observed, issuer = capacity_harness
    order = create_order(client, auth).json()
    path = PREFIX + '/orders/' + order['id']
    client.post(path + '/confirm-test', headers=auth)
    with client.session_factory.begin() as db:
        db.get(CapacityOrder, order['id']).started_at = now() - timedelta(hours=1)
    provider = provider_for(issuer, ResizeHTTP(issuer))
    try:
        reconcile_capacity(client.session_factory, provider, env_id, get_settings(), probe=lambda: observed)
        result = client.get(PREFIX, headers=auth).json()
        assert result['plan']['storage_gib'] == 2 and result['active_order']['status'] == 'failed'
        retried = client.post(path + '/retry', headers=auth).json()
        assert retried['payment_status'] == 'confirmed' and retried['attempt'] == 2
        assert retried['plan'] == order['plan']
        provider.client.close()
        bad = ResizeHTTP(issuer)
        bad.objects['PersistentVolume']['metadata']['uid'] = 'replacement'
        provider.client = httpx.Client(base_url='http://127.0.0.1:1/', transport=httpx.MockTransport(bad))
        reconcile_capacity(client.session_factory, provider, env_id, get_settings(), probe=lambda: observed)
        assert client.post(path + '/retry', headers=auth).status_code == 409
        assert not bad.calls
    finally:
        provider.close()


def test_application_creation_uses_activated_account_limit(capacity_harness, monkeypatch):
    client, auth, env_id, _, _ = capacity_harness
    monkeypatch.setenv('PLATFORM_PUBLISHER_ENABLED', 'true')
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.applications.call_files', lambda *args: httpx.Response(200, json={'entries': [{'name': 'Dockerfile', 'kind': 'file'}]}))
    client.get(PREFIX, headers=auth)
    with client.session_factory.begin() as db:
        for index in range(3):
            db.add(UserApplication(environment_id=env_id, project_path=f'project-{index}', status='running'))
    url = '/internal/v1/workspace/apps?path=project-new'
    assert client.post(url, headers=auth).status_code == 409
    with client.session_factory.begin() as db:
        account = db.get(CapacityAccount, env_id)
        account.plan = {**account.plan, 'application_limit': 5, 'build_concurrency': 4,
                        'applications_cpu_millicores': 1250, 'applications_memory_mib': 1280}
    assert client.post(url, headers=auth).status_code == 202
    assert client.post(url.replace('project-new', 'project-last'), headers=auth).status_code == 202
    assert client.post(url.replace('project-new', 'project-excess'), headers=auth).status_code == 409
    with client.session_factory.begin() as db:
        db.add(UserApplication(environment_id=env_id, project_path='project-deleted', status='deleted'))
    restored = url.replace('project-new', 'project-deleted')
    assert client.post(restored, headers=auth).status_code == 409
    with client.session_factory.begin() as db:
        db.scalar(select(UserApplication).where(UserApplication.environment_id == env_id,
                  UserApplication.project_path == 'project-0')).status = 'deleted'
    assert client.post(restored, headers=auth).status_code == 202
