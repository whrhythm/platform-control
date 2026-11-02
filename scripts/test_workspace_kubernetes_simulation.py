"""Exercise the actual provider against a local Postman simulation."""
from copy import deepcopy
import os
import secrets
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from control_plane.kubernetes_workspace_provider import KubernetesProviderError, KubernetesWorkspaceProvider
from control_plane.models import Base, Tenant, UserEnvironment
from control_plane.workspace_provisioning import request_intent, provision_one

subject = 'sim-' + secrets.token_hex(6)
provider = KubernetesWorkspaceProvider(api_url=os.environ['K8S_SIM_URL'], token='synthetic-test-token',
                                       issuer='test-issuer', subject=subject, storage_gib=2,
                                       allow_test_loopback_http=True, timeout_seconds=2)
try:
    objects = provider.objects
    ns = provider.create_namespace(objects['Namespace'])
    assert ns['metadata']['uid']
    assert provider.read_namespace(provider.namespace)['metadata']['uid'] == ns['metadata']['uid']
    assert provider.create_network_policy(objects['NetworkPolicy'])['kind'] == 'NetworkPolicy'
    assert provider.create_resource_quota(objects['ResourceQuota'])['kind'] == 'ResourceQuota'
    assert provider.create_pvc(objects['PersistentVolumeClaim'])['kind'] == 'PersistentVolumeClaim'
    try:
        provider.create_namespace(objects['Namespace'])
    except KubernetesProviderError as exc:
        assert '409' in str(exc) and 'synthetic-test-token' not in str(exc)
    else:
        raise AssertionError('conflicting namespace was adopted')
    altered = deepcopy(objects['PersistentVolumeClaim'])
    altered['spec']['storageClassName'] = 'longhorn'
    try:
        provider.create_pvc(altered)
    except ValueError:
        pass
    else:
        raise AssertionError('unapproved manifest was sent')
    print('PASS: create, read, quota, isolation, PVC, conflict and manifest restriction')
finally:
    provider.close()

# Execute the durable journal against the real HTTP adapter and mock server.
with TemporaryDirectory() as directory:
    engine = create_engine('sqlite:///' + directory.replace('\\', '/') + '/state.db')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    subject = 'journal-' + secrets.token_hex(6)
    with sessions.begin() as db:
        tenant = Tenant(name='test', owner_issuer='test-issuer', owner_subject=subject, project_limit=1)
        db.add(tenant)
        db.flush()
        env = UserEnvironment(issuer='test-issuer', subject=subject, identity_hash=subject, storage_gib=2,
                              first_tenant_id=tenant.id, first_project_id='test-project')
        db.add(env)
        db.flush()
        environment_id = env.id
    journal_provider = KubernetesWorkspaceProvider(api_url=os.environ['K8S_SIM_URL'], token='synthetic-test-token',
                                                   issuer='test-issuer', subject=subject, storage_gib=2,
                                                   allow_test_loopback_http=True, timeout_seconds=2)
    try:
        assert request_intent(sessions, environment_id) == 'requested'
        states = [provision_one(sessions, journal_provider, environment_id) for _ in range(4)]
        assert states == ['namespace_recorded', 'quota_recorded', 'policy_recorded', 'pvc_recorded']
        print('PASS: durable journal over simulated HTTP in isolation-before-PVC order')
    finally:
        journal_provider.close()
        engine.dispose()
