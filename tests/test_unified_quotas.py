from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from control_plane.capacity import CapacityPlan
from control_plane.capacity_controller import reconcile_capacity
from control_plane.model_billing_controller import apply_credit
from control_plane.models import CapacityAccount, UserApplication, UserEnvironment, AgentTask
from control_plane.settings import get_settings
from control_plane.plan_quotas import application_allocations
from control_plane.application_runtime import build_objects
from control_plane.agent_runtime import command_job
from test_capacity import capacity_harness, create_order, PREFIX, ResizeHTTP, provider_for, GIB
from test_model_membership import membership
from test_project_databases import databases, request as request_database
from test_agent_workbench import workspace, create


def test_admin_price_credit_snapshot_and_full_activation(membership, harness, monkeypatch):
    client, auth, env, key, state, gateway = membership
    _, token = harness
    monkeypatch.setattr('control_plane.ai_policy.provider_catalog', lambda s: [{'name': 'model'}])
    admin = {'Authorization': 'Bearer ' + token('operator', groups=['platform-admins'])}
    policy = client.get('/v1/admin/ai', headers=admin).json()['policy']
    policy['plans'][0].update(model_credit_cny_minor=500, database_budget_mib=512,
        database_per_instance_mib=256, database_limit=2, applications_cpu_millicores=1000,
        applications_memory_mib=1024, agent_cpu_millicores=750, agent_memory_mib=768,
        agent_command_seconds=300, agent_task_seconds=2400, build_timeout_seconds=1200)
    assert client.put('/v1/admin/ai', headers=admin, json={'revision': 0, 'policy': policy}).status_code == 200
    before = client.get(PREFIX, headers=auth).json()
    assert before['plan']['database_budget_mib'] == 256
    order = create_order(client, auth).json()
    assert order['plan']['amount_minor'] == 1000
    assert order['plan']['model_credit_usd_micros'] == 714285
    policy['plans'][0]['model_credit_cny_minor'] = 100
    policy['plans'][0]['agent_cpu_millicores'] = 1000
    assert client.put('/v1/admin/ai', headers=admin, json={'revision': 1, 'policy': policy}).status_code == 200
    assert create_order(client, auth).status_code == 409  # same key, changed quote
    path = PREFIX + '/orders/' + order['id'] + '/confirm-test'
    assert client.post(path, headers=auth).status_code == 200
    transport = ResizeHTTP(env.issuer)
    provider = provider_for(env.issuer, transport)
    observed = {'physical_total_bytes': GIB * 38 // 10}
    try:
        run = lambda: reconcile_capacity(client.session_factory, provider, env.id, get_settings(), probe=lambda: observed)
        run()  # Credit must arrive before resource activation.
        assert client.get(PREFIX, headers=auth).json()['plan']['agent_cpu_millicores'] == 250
        for _ in range(6):
            apply_credit(client.session_factory, order['id'], gateway, get_settings())
        assert state['user']['max_budget'] == state['info']['max_budget'] == 1.714285
        for _ in range(4):
            run()
        transport.objects['PersistentVolumeClaim']['status']['capacity']['storage'] = '4Gi'
        transport.objects['PersistentVolume']['spec']['capacity']['storage'] = '4Gi'
        # Establish real observed growth relative to the durable initial measurement.
        observed['physical_total_bytes'] = 4 * GIB
        run()
        after = client.get(PREFIX, headers=auth).json()
        assert after['active_order'] is None
        assert after['plan']['database_budget_mib'] == 512
        assert after['plan']['agent_cpu_millicores'] == 750  # frozen order, not edited catalog
        assert after['plan']['applications_cpu_millicores'] == 1000
        assert client.post(path, headers=auth).json()['attempt'] == 1
        assert len(state['writes']) == 2
    finally:
        provider.close()


def test_application_total_and_build_admission_use_active_plan(capacity_harness, monkeypatch):
    client, auth, env_id, *_ = capacity_harness
    monkeypatch.setenv('PLATFORM_PUBLISHER_ENABLED', 'true')
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.applications.call_files', lambda *a: httpx.Response(200,
        json={'entries': [{'name': 'Dockerfile', 'kind': 'file'}]}))
    client.get(PREFIX, headers=auth)
    with client.session_factory.begin() as db:
        account = db.get(CapacityAccount, env_id)
        account.plan = {**account.plan, 'applications_cpu_millicores': 250, 'applications_memory_mib': 256, 'build_concurrency': 1}
    assert client.post('/internal/v1/workspace/apps?path=one', headers=auth).status_code == 202
    assert client.post('/internal/v1/workspace/apps?path=two', headers=auth).status_code == 409
    with client.session_factory.begin() as db:
        app = db.scalar(select(UserApplication).where(UserApplication.environment_id == env_id))
        app.status, app.image = 'running', 'registry/test@sha256:' + 'a' * 64
        identifier = app.id
    assert client.post('/internal/v1/workspace/apps?path=two', headers=auth).json()['detail'] == 'application_compute_allowance_exceeded'
    assert client.post(f'/internal/v1/workspace/apps/{identifier}/stop', headers=auth).status_code == 202
    assert client.post('/internal/v1/workspace/apps?path=two', headers=auth).status_code == 409  # not yet observed stopped
    with client.session_factory.begin() as db:
        db.get(UserApplication, identifier).status = 'stopped'
    assert client.post('/internal/v1/workspace/apps?path=two', headers=auth).status_code == 202
    assert client.post(f'/internal/v1/workspace/apps/{identifier}/start', headers=auth).status_code == 409


def test_database_allocation_uses_snapshot_not_global_defaults(databases):
    client, auth = databases
    client.get(PREFIX, headers=auth)
    with client.session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.subject == 'alice'))
        account = db.get(CapacityAccount, env.id)
        account.plan = {**account.plan, 'database_budget_mib': 512, 'database_per_instance_mib': 256, 'database_limit': 2}
    assert request_database(client, auth, 'one').json()['reserved_bytes'] == 256 * 1024 ** 2
    assert request_database(client, auth, 'two').status_code == 202
    assert request_database(client, auth, 'three').status_code == 409
    assert client.get('/internal/v1/workspace/databases', headers=auth).json()['budget_bytes'] == 512 * 1024 ** 2


def test_agent_overlap_and_quota_snapshot(workspace):
    client, headers, base = workspace
    with client.session_factory.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.subject == 'alice'))
        db.add(CapacityAccount(environment_id=env.id, plan={'id': 'basic', 'agent_concurrency': 2,
            'agent_cpu_millicores': 750, 'agent_memory_mib': 768, 'agent_command_seconds': 90}))
    task = create(workspace)
    assert task.config['quota']['agent_cpu_millicores'] == 750
    with pytest.raises(HTTPException):
        create(workspace, 'same project must remain serialized')
    config = client.get(base + '/preferences', headers=headers).json()
    config['project_path'] = 'another'
    assert client.put(base + '/preferences', headers=headers, json=config).status_code == 200
    assert create(workspace, 'another project').status == 'queued'
    with pytest.raises(HTTPException):
        create(workspace, 'third task')


@pytest.mark.parametrize('change', [
    {'applications_cpu_millicores': 100}, {'database_budget_mib': 8192},
    {'database_budget_mib': 128, 'database_per_instance_mib': 256},
    {'agent_command_seconds': 1800, 'agent_task_seconds': 60}, {'build_memory_mib': 512},
])
def test_invalid_cross_resource_quotas_rejected(change):
    with pytest.raises(ValueError):
        CapacityPlan.model_validate(dict(id='basic', version='v1', name='test', storage_gib=4,
            application_limit=5, amount_minor=1000, currency='CNY', **change))
