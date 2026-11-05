"""Trial catalog edits freeze at first provisioning, never at every login."""
from copy import deepcopy

from control_plane.auth import Principal
from control_plane.main import ensure_personal_environment
from control_plane.models import CapacityAccount
from test_user_environments_api import setup_project, environment_url


def admin_policy(client, token, monkeypatch):
    monkeypatch.setattr('control_plane.ai_policy.provider_catalog', lambda s: [{'name': 'model'}])
    admin = {'Authorization': 'Bearer ' + token('operator', groups=['platform-admins'])}
    response = client.get('/v1/admin/ai', headers=admin).json()
    policy = response['policy']
    policy.update(chat_model='model', coding_model='model', allowed_models=['model'])
    return admin, policy


def test_trial_catalog_validation_authorization_and_legacy_clients(harness, monkeypatch):
    client, token = harness
    admin, policy = admin_policy(client, token, monkeypatch)
    assert policy['trial_plan']['id'] == 'trial'
    for changes in ({'amount_minor': 100}, {'model_credit_cny_minor': 100}, {'id': 'basic'},
                    {'database_budget_mib': 40960}, {'agent_command_seconds': 1800, 'agent_task_seconds': 60}):
        bad = deepcopy(policy)
        bad['trial_plan'].update(changes)
        assert client.put('/v1/admin/ai', headers=admin, json={'revision': 0, 'policy': bad}).status_code == 422
    policy['trial_plan'].update(storage_gib=3, code_runs_monthly=42)
    assert client.put('/v1/admin/ai', headers={'Authorization': 'Bearer ' + token('alice')}, json={'revision': 0, 'policy': policy}).status_code == 403
    assert client.put('/v1/admin/ai', headers=admin, json={'revision': 0, 'policy': policy}).status_code == 200
    assert client.put('/v1/admin/ai', headers=admin, json={'revision': 0, 'policy': policy}).status_code == 409
    policy.pop('trial_plan')
    saved = client.put('/v1/admin/ai', headers=admin, json={'revision': 1, 'policy': policy})
    assert saved.status_code == 200
    assert saved.json()['policy']['trial_plan']['storage_gib'] == 3


def test_new_login_freezes_trial_and_repeated_login_preserves_it(harness, monkeypatch):
    client, token = harness
    admin, policy = admin_policy(client, token, monkeypatch)
    alice, tenant_id, _ = setup_project(client, token)
    principal = Principal(issuer='https://identity.example.test/application/o/platform/', subject='alice')
    with client.session_factory() as db:
        original = ensure_personal_environment(principal, tenant_id, db)
        env_id = original.id
        before = deepcopy(db.get(CapacityAccount, env_id).plan)
    policy['trial_plan'].update(storage_gib=3, code_runs_monthly=42, application_cpu_millicores=200)
    assert client.put('/v1/admin/ai', headers=admin, json={'revision': 0, 'policy': policy}).status_code == 200
    with client.session_factory() as db:
        repeated = ensure_personal_environment(principal, tenant_id, db)
        assert repeated.id == env_id
        assert repeated.storage_gib == before['storage_gib']
        assert db.get(CapacityAccount, env_id).plan == before
    bob, other_tenant, _ = setup_project(client, token, user='bob')
    with client.session_factory() as db:
        new = ensure_personal_environment(Principal(issuer=principal.issuer, subject='bob'), other_tenant, db)
        assert new.storage_gib == 3
        assert new.identity_hash != repeated.identity_hash
        new_plan = db.get(CapacityAccount, new.id).plan
        assert new_plan['code_runs_monthly'] == 42
        assert new_plan['application_cpu_millicores'] == 200
    # The older project API must freeze exactly the same rights too.
    auth, tenant, project = setup_project(client, token, user='carol')
    env = client.post(environment_url(tenant, project), headers=auth).json()
    with client.session_factory() as db:
        assert db.get(CapacityAccount, env['id']).plan == new_plan
