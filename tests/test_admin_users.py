from datetime import timedelta
from uuid import uuid4

from control_plane.models import (
    CapacityAccount, ModelWallet, StorageMeasurement, Tenant, UserApplication, UserEnvironment,
    WorkspaceFileService, WorkspaceVolumeBinding, now,
)
from control_plane.user_environments import identity_digest
from test_api import headers


def test_admin_inventory_lists_scoped_samples_without_provisioning_or_exposing_secrets(harness):
    client, token = harness
    admin = headers(token('operator', groups=['platform-admins']))
    regular = headers(token('alice'))
    base = '/v1/admin/users'
    at = now()
    old = at - timedelta(hours=1)
    issuer = 'https://identity.example.test/application/o/platform/'
    ids = {}
    with client.session_factory.begin() as db:
        for subject in ('alice_%', 'alice-other'):
            tenant = Tenant(name='团队甲' if subject == 'alice_%' else '团队乙',
                            owner_issuer=issuer, owner_subject=subject, project_limit=3)
            db.add(tenant)
            db.flush()
            env = UserEnvironment(issuer=issuer, subject=subject, identity_hash=identity_digest(issuer, subject),
                                  first_tenant_id=tenant.id, storage_gib=4 if subject == 'alice_%' else 2,
                                  created_at=at if subject == 'alice_%' else old)
            db.add(env)
            db.flush()
            ids[subject] = env.id
        owned, other = ids['alice_%'], ids['alice-other']
        db.add_all([
            CapacityAccount(environment_id=owned, plan={'id': 'basic', 'name': '入门', 'application_limit': 5,
                                                        'private': 'fixture-internal-plan'}),
            WorkspaceFileService(environment_id=owned, status='ready', encrypted_token='fixture-file-token'),
            WorkspaceVolumeBinding(environment_id=owned, namespace_name='codechat-user-synthetic',
                                   namespace_uid='fixture-ns-uid', pvc_uid='fixture-pvc-uid',
                                   pv_name='fixture-pv', pv_uid='fixture-pv-uid', volume_handle='fixture-disk'),
            StorageMeasurement(environment_id=owned, component='files', used_bytes=0, observed_at=old, status='ok'),
            StorageMeasurement(environment_id=owned, component='images', used_bytes=300, observed_at=at, status='ok'),
            ModelWallet(environment_id=owned, status='ok', observed_at=at,
                        sample={'allocated_usd_micros': 2_000_000, 'spent_usd_micros': 500_000,
                                'remaining_usd_micros': 1_500_000, 'total_tokens': 123, 'key': 'fixture-private-key'}),
            ModelWallet(environment_id=other, status='ok', sample={'remaining_usd_micros': 900}),
            UserApplication(environment_id=owned, project_path='latest', status='running', created_at=at,
                            observation={'logs': 'fixture-private-log'}, resources={'secret': 'fixture-private-resource'}),
            UserApplication(environment_id=owned, project_path='earlier', status='stopped', created_at=old),
            UserApplication(environment_id=owned, project_path='deleted', status='deleted', created_at=at),
            UserApplication(environment_id=other, project_path='other-user-app', status='running', created_at=at),
        ])

    detail_path = base + '/' + owned
    assert client.get(base).status_code == 401
    assert client.get(base, headers=regular).status_code == 403
    assert client.get(detail_path, headers=regular).status_code == 403
    response = client.get(base, headers=admin, params={'limit': 1})
    assert response.status_code == 200
    first = response.json()
    assert first['next_offset'] == 1
    user, = first['items']
    assert user['subject'] == 'alice_%' and user['environment_id'] == owned
    assert user['plan_name'] == '入门' and user['application_count'] == 2 and user['application_limit'] == 5
    assert user['file_service_status'] == 'ready' and user['namespace'] == 'codechat-user-synthetic'
    assert user['files']['used_bytes'] == 0 and user['files']['budget_bytes'] == 4 * 2 ** 30
    assert user['files']['status'] == 'stale' and user['files']['observed_at'].endswith('Z')
    assert user['model_usage']['remaining_usd_micros'] == 1_500_000
    assert user['model_usage']['total_tokens'] == 123
    next_page = client.get(base, headers=admin, params={'offset': first['next_offset']}).json()
    assert [item['environment_id'] for item in next_page['items']] == [other]
    absent = next_page['items'][0]
    assert absent['plan_id'] is None and absent['files']['used_bytes'] is None
    assert absent['model_usage']['status'] == 'unknown' and absent['model_usage']['remaining_usd_micros'] is None
    assert next_page['next_offset'] is None
    for q in ('_%', '团队甲', owned, 'codechat-user-synthetic'):
        found = client.get(base, headers=admin, params={'q': q}).json()
        assert [item['environment_id'] for item in found['items']] == [owned]

    response = client.get(detail_path, headers=admin, params={'application_limit': 1})
    assert response.status_code == 200
    detail = response.json()
    assert detail['user'] == user
    assert [app['project'] for app in detail['applications']] == ['latest']
    assert detail['next_application_offset'] == 1
    second = client.get(detail_path, headers=admin, params={'application_offset': 1}).json()
    assert [app['project'] for app in second['applications']] == ['earlier']
    storage = {sample['component']: sample for sample in detail['storage']}
    assert len(storage) == 5
    assert storage['images']['used_bytes'] == 300 and storage['images']['budget_bytes'] is None
    assert storage['objects']['used_bytes'] is None and storage['objects']['status'] == 'unknown'
    for value in ('fixture-file-token', 'fixture-private-key', 'fixture-private-log', 'fixture-internal-plan',
                  'fixture-private-resource', 'fixture-disk', 'fixture-pvc-uid'):
        assert value not in response.text
    assert client.get(base + '/' + str(uuid4()), headers=admin).status_code == 404
    assert client.post(detail_path, headers=admin, json={'status': 'suspended'}).status_code == 405
    with client.session_factory() as db:
        assert db.get(CapacityAccount, other) is None
        assert db.get(UserEnvironment, owned).status == 'requested'
