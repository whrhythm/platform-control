"""Real HTTP adapter path against the repository's existing Postman model."""
import os
import secrets

import pytest
from sqlalchemy.orm import sessionmaker

from control_plane.capacity import GIB
from control_plane.capacity_controller import reconcile_capacity
from control_plane.database import make_engine
from control_plane.kubernetes_workspace_provider import KubernetesWorkspaceProvider
from control_plane.models import Base, CapacityAccount, CapacityOrder, Tenant, UserEnvironment, WorkspaceVolumeBinding, now
from control_plane.settings import Settings
from control_plane.user_environments import identity_digest
from control_plane.workspace_provisioning import request_intent, provision_one, observe_and_record_volume


def test_expansion_uses_same_volume_and_waits_for_guest_filesystem_over_real_http(tmp_path):
    url = os.environ.get('CODECHAT_K8S_SIM_URL')
    if not url:
        pytest.skip('requires local Postman Kubernetes simulation')
    subject = 'capacity-' + secrets.token_hex(6)
    engine = make_engine('sqlite:///' + str(tmp_path / 'capacity.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        tenant = Tenant(name='test', owner_issuer='test-issuer', owner_subject=subject, project_limit=1)
        db.add(tenant)
        db.flush()
        env = UserEnvironment(issuer='test-issuer', subject=subject, identity_hash=identity_digest('test-issuer', subject), storage_gib=2, first_tenant_id=tenant.id)
        db.add(env)
        db.flush()
        env_id = env.id
    provider = KubernetesWorkspaceProvider(api_url=url, token='synthetic-token', issuer='test-issuer', subject=subject, storage_gib=2, allow_test_loopback_http=True)
    try:
        request_intent(sessions, env_id)
        for _ in range(4):
            provision_one(sessions, provider, env_id)
        assert observe_and_record_volume(sessions, provider, env_id) == 'bound'
        with sessions.begin() as db:
            pvc_uid = db.get(WorkspaceVolumeBinding, env_id).pvc_uid
            plan = {'id': 'basic', 'version': 'synthetic', 'name': 'Basic', 'storage_gib': 4, 'application_limit': 5, 'amount_minor': 1000, 'currency': 'CNY'}
            order = CapacityOrder(environment_id=env_id, idempotency_key='synthetic-capacity-key', plan=plan, status='applying', payment_status='confirmed', attempt=1, started_at=now())
            db.add(order)
            db.flush()
            db.add(CapacityAccount(environment_id=env_id, active_order_id=order.id, plan={**plan, 'storage_gib': 2, 'application_limit': 3}))
        settings = Settings()
        probe = {'physical_total_bytes': GIB * 19 // 10}
        for _ in range(4):
            assert reconcile_capacity(sessions, provider, env_id, settings, probe=lambda: probe)
        with sessions() as db:
            assert db.get(UserEnvironment, env_id).storage_gib == 2
        probe['physical_total_bytes'] = GIB * 38 // 10
        reconcile_capacity(sessions, provider, env_id, settings, probe=lambda: probe)
        with sessions() as db:
            assert db.get(UserEnvironment, env_id).storage_gib == 4
            assert db.get(WorkspaceVolumeBinding, env_id).pvc_uid == pvc_uid
            assert db.get(CapacityAccount, env_id).active_order_id is None
        pvc, _ = provider.inspect_claim_volume(pvc_uid)
        assert pvc['spec']['resources']['requests']['storage'] == '4Gi'
    finally:
        provider.close()
        engine.dispose()
