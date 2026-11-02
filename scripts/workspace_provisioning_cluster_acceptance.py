"""Exercise journal + real API with one synthetic volume and guarded cleanup."""
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from control_plane.kubernetes_workspace_provider import KubernetesWorkspaceProvider
from control_plane.models import Base, Tenant, UserEnvironment, WorkspaceProvisioningIntent
from control_plane.workspace_provisioning import provision_one, request_intent

binary = os.environ.get('KUBECTL_BIN') or shutil.which('kubectl')
if not binary:
    raise RuntimeError('kubectl not found')
subject = 'cluster-journal-qa-' + secrets.token_hex(12)
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
base = f'http://127.0.0.1:{port}'
proxy = subprocess.Popen([binary, '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER'],
                          'proxy', '--address=127.0.0.1', '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
provider = None
namespace = namespace_uid = pvc_uid = pv_name = pv_uid = volume = None


def wait_for(fn, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = fn()
        if result:
            return result
        time.sleep(1)
    raise RuntimeError('Timed out waiting for test resource')


try:
    with httpx.Client(timeout=5, trust_env=False) as client, tempfile.TemporaryDirectory() as directory:
        def proxy_ready():
            try:
                return client.get(base + '/version').status_code == 200
            except httpx.HTTPError:
                return False
        wait_for(proxy_ready, 20)
        engine = create_engine('sqlite:///' + directory + '/journal.db')
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        with sessions.begin() as db:
            tenant = Tenant(name='qa', owner_issuer='cluster-qa', owner_subject=subject, project_limit=1)
            db.add(tenant)
            db.flush()
            env = UserEnvironment(issuer='cluster-qa', subject=subject, identity_hash=subject, storage_gib=2,
                                  first_tenant_id=tenant.id, first_project_id='qa')
            db.add(env)
            db.flush()
            environment_id = env.id
        provider = KubernetesWorkspaceProvider(api_url=base, token='qa-proxy-placeholder', issuer='cluster-qa',
                                               subject=subject, storage_gib=2, allow_test_loopback_http=True)
        namespace = provider.namespace
        request_intent(sessions, environment_id)
        assert provision_one(sessions, provider, environment_id) == 'namespace_recorded'
        with sessions() as db:
            namespace_uid = db.get(WorkspaceProvisioningIntent, environment_id).namespace_uid
        assert provision_one(sessions, provider, environment_id) == 'quota_recorded'
        assert provision_one(sessions, provider, environment_id) == 'policy_recorded'
        wait_for(lambda: client.get(f'{base}/api/v1/namespaces/{namespace}/resourcequotas/user-capacity').json().get('status', {}).get('hard'))
        assert provision_one(sessions, provider, environment_id) == 'pvc_recorded'
        with sessions() as db:
            pvc_uid = db.get(WorkspaceProvisioningIntent, environment_id).pvc_uid

        def get_bound():
            response = client.get(f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims/workspace-data')
            response.raise_for_status()
            obj = response.json()
            if obj['metadata']['uid'] != pvc_uid:
                raise RuntimeError('Test PVC UID changed')
            return obj if obj.get('status', {}).get('phase') == 'Bound' else None
        claim = wait_for(get_bound)
        pv_name = claim['spec']['volumeName']
        response = client.get(f'{base}/api/v1/persistentvolumes/{pv_name}')
        response.raise_for_status()
        pv = response.json()
        pv_uid = pv['metadata']['uid']
        ref = pv['spec']['claimRef']
        assert ref['uid'] == pvc_uid and ref['namespace'] == namespace and ref['name'] == 'workspace-data'
        assert pv['spec']['persistentVolumeReclaimPolicy'] == 'Retain'
        assert pv['spec']['csi']['driver'] == 'driver.longhorn.io'
        volume = pv['spec']['csi']['volumeHandle']
        print('PASS: journal created isolated test namespace, quota, policy and bound retained Longhorn volume')
        engine.dispose()
finally:
    if provider:
        provider.close()
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            if namespace and namespace_uid:
                current = client.get(f'{base}/api/v1/namespaces/{namespace}')
                current.raise_for_status()
                if current.json()['metadata']['uid'] != namespace_uid:
                    raise RuntimeError('Test namespace changed; refusing cleanup')
                claims = client.get(f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims')
                claims.raise_for_status()
                items = claims.json()['items']
                if items:
                    if len(items) != 1 or items[0]['metadata']['uid'] != pvc_uid or not pv_uid or not volume:
                        raise RuntimeError('Test PVC outcome uncertain; preserve namespace for review: ' + namespace)
                    response = client.request('DELETE', f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims/workspace-data',
                                              json={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': pvc_uid}})
                    if response.status_code not in (200, 202):
                        raise RuntimeError('Test PVC deletion failed')
                    wait_for(lambda: client.get(f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims/workspace-data').status_code == 404)
                    def released():
                        obj = client.get(f'{base}/api/v1/persistentvolumes/{pv_name}')
                        obj.raise_for_status()
                        pv = obj.json()
                        if pv['metadata']['uid'] != pv_uid or pv['spec']['claimRef']['uid'] != pvc_uid:
                            raise RuntimeError('Test PV identity changed; refusing cleanup')
                        return pv.get('status', {}).get('phase') == 'Released'
                    wait_for(released)
                    operations = [{'op': 'test', 'path': '/metadata/uid', 'value': pv_uid},
                                  {'op': 'test', 'path': '/spec/claimRef/uid', 'value': pvc_uid},
                                  {'op': 'replace', 'path': '/spec/persistentVolumeReclaimPolicy', 'value': 'Delete'}]
                    response = client.patch(f'{base}/api/v1/persistentvolumes/{pv_name}', json=operations,
                                            headers={'Content-Type': 'application/json-patch+json'})
                    response.raise_for_status()
                    wait_for(lambda: client.get(f'{base}/api/v1/persistentvolumes/{pv_name}').status_code == 404, 180)
                    wait_for(lambda: client.get(f'{base}/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/{volume}').status_code == 404, 180)
                response = client.request('DELETE', f'{base}/api/v1/namespaces/{namespace}',
                                          json={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': namespace_uid}})
                if response.status_code not in (200, 202):
                    raise RuntimeError('Test namespace deletion failed')
                wait_for(lambda: client.get(f'{base}/api/v1/namespaces/{namespace}').status_code == 404, 180)
                print('PASS: test PVC, PV, Longhorn volume and namespace safely cleaned')
            elif namespace:
                print('REVIEW: namespace creation outcome uncertain:', namespace)
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait()
