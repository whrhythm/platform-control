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
from control_plane.kubernetes_workspace_provider import KubernetesProviderError
from control_plane.models import Base, Tenant, UserEnvironment, WorkspaceProvisioningIntent, WorkspaceVolumeBinding
from control_plane.workspace_provisioning import provision_one, request_intent, observe_and_record_volume
from control_plane.user_environments import identity_digest

binary = os.environ.get('KUBECTL_BIN') or shutil.which('kubectl')
if not binary:
    raise RuntimeError('kubectl not found')
qa_run = secrets.token_hex(12)
subject = 'cluster-journal-qa-' + qa_run
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
base = f'http://127.0.0.1:{port}'
proxy = subprocess.Popen([binary, '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER'],
                          'proxy', '--address=127.0.0.1', '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
provider = None
namespace = namespace_uid = pvc_uid = pv_name = pv_uid = volume = pod_uid = None
pvc_uids = set()


def wait_for(fn, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = fn()
        if result:
            return result
        time.sleep(1)
    raise RuntimeError('Timed out waiting for test resource')


def execute(program):
    result = subprocess.run([binary, '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER'],
                             '-n', namespace, 'exec', 'workspace-qa', '--', 'python', '-c', program],
                            text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Synthetic workspace command failed: ' + result.stderr[-600:])


def create_pod(client):
    pod = {'apiVersion': 'v1', 'kind': 'Pod',
           'metadata': {'name': 'workspace-qa', 'namespace': namespace, 'labels': {'codechat.io/purpose': 'workspace-persistence-test'}},
           'spec': {'runtimeClassName': 'kata-qemu-runtime-rs', 'automountServiceAccountToken': False,
                    'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
                                        'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
                    'containers': [{'name': 'runtime', 'image': 'ghcr.nju.edu.cn/openhands/agent-canvas@sha256:638c5e3ce76df74344bb18bd85f44af395337ade8e71f04532099fb380febadf',
                                    'command': ['python', '-c', 'import time; time.sleep(900)'],
                                    'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                                    'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}},
                                    'volumeMounts': [{'name': 'workspace', 'mountPath': '/workspace'}]}],
                    'volumes': [{'name': 'workspace', 'persistentVolumeClaim': {'claimName': 'workspace-data'}}]}}
    response = client.post(f'{base}/api/v1/namespaces/{namespace}/pods', json=pod)
    response.raise_for_status()
    uid = response.json()['metadata']['uid']
    def ready():
        response = client.get(f'{base}/api/v1/namespaces/{namespace}/pods/workspace-qa')
        response.raise_for_status()
        obj = response.json()
        if obj['metadata']['uid'] != uid:
            raise RuntimeError('Test pod UID changed')
        return any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in obj.get('status', {}).get('conditions', []))
    wait_for(ready, 180)
    return uid


def delete_pod(client, uid):
    response = client.request('DELETE', f'{base}/api/v1/namespaces/{namespace}/pods/workspace-qa',
                              json={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}})
    if response.status_code not in (200, 202):
        raise RuntimeError('Test pod deletion failed')
    wait_for(lambda: client.get(f'{base}/api/v1/namespaces/{namespace}/pods/workspace-qa').status_code == 404, 180)


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
            env = UserEnvironment(issuer='cluster-qa', subject=subject, identity_hash=identity_digest('cluster-qa', subject), storage_gib=2,
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
        response = client.patch(f'{base}/api/v1/namespaces/{namespace}', json=[
            {'op': 'test', 'path': '/metadata/uid', 'value': namespace_uid},
            {'op': 'add', 'path': '/metadata/labels/codechat.io~1qa-run', 'value': qa_run},
            {'op': 'add', 'path': '/metadata/labels/codechat.io~1purpose', 'value': 'workspace-persistence-test'},
        ], headers={'Content-Type': 'application/json-patch+json'})
        response.raise_for_status()
        print('TEST namespace:', namespace, 'UID:', namespace_uid, 'run:', qa_run, flush=True)
        assert provision_one(sessions, provider, environment_id) == 'quota_recorded'
        assert provision_one(sessions, provider, environment_id) == 'policy_recorded'
        wait_for(lambda: client.get(f'{base}/api/v1/namespaces/{namespace}/resourcequotas/user-capacity').json().get('status', {}).get('hard'))
        assert provision_one(sessions, provider, environment_id) == 'pvc_recorded'
        with sessions() as db:
            pvc_uid = db.get(WorkspaceProvisioningIntent, environment_id).pvc_uid
            pvc_uids.add(pvc_uid)

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
        labels = {**pv['metadata'].get('labels', {}), 'codechat.io/qa-run': qa_run,
                  'codechat.io/purpose': 'workspace-persistence-test'}
        response = client.patch(f'{base}/api/v1/persistentvolumes/{pv_name}', json=[
            {'op': 'test', 'path': '/metadata/uid', 'value': pv_uid},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': pv['metadata']['resourceVersion']},
            {'op': 'add', 'path': '/metadata/labels', 'value': labels},
        ], headers={'Content-Type': 'application/json-patch+json'})
        response.raise_for_status()
        print('TEST PV:', pv_name, 'UID:', pv_uid, 'Longhorn volume:', volume, flush=True)
        assert observe_and_record_volume(sessions, provider, environment_id) == 'bound'
        assert observe_and_record_volume(sessions, provider, environment_id) == 'bound'
        with sessions() as db:
            binding = db.get(WorkspaceVolumeBinding, environment_id)
            assert (binding.namespace_uid, binding.pvc_uid, binding.pv_uid, binding.volume_handle) == (namespace_uid, pvc_uid, pv_uid, volume)
            assert db.get(UserEnvironment, environment_id).status == 'requested'
        print('PASS: journal created isolated test resources and recorded verified retained volume binding')
        pod_uid = create_pod(client)
        execute("import os; p='/workspace/persistence-check.txt'; f=open(p,'wb'); f.write(b'codechat-test-data'); f.flush(); os.fsync(f.fileno()); f.close()")
        previous_pod_uid = pod_uid
        delete_pod(client, pod_uid)
        pod_uid = None
        assert observe_and_record_volume(sessions, provider, environment_id) == 'bound'
        pod_uid = create_pod(client)
        assert pod_uid != previous_pod_uid
        execute("assert open('/workspace/persistence-check.txt','rb').read()==b'codechat-test-data'")
        assert observe_and_record_volume(sessions, provider, environment_id) == 'bound'
        print('PASS: synthetic file survives Kata pod stop and replacement on the same retained PVC')
        delete_pod(client, pod_uid)
        pod_uid = None
        old_pvc_uid = pvc_uid
        response = client.request('DELETE', f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims/workspace-data',
                                  json={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': old_pvc_uid}})
        if response.status_code not in (200, 202):
            raise RuntimeError('Test PVC deletion failed')
        wait_for(lambda: client.get(f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims/workspace-data').status_code == 404)
        def retained_pv():
            response = client.get(f'{base}/api/v1/persistentvolumes/{pv_name}')
            response.raise_for_status()
            obj = response.json()
            if (obj['metadata']['uid'] != pv_uid or obj['spec']['claimRef']['uid'] != old_pvc_uid or
                    obj['spec']['csi']['volumeHandle'] != volume or obj['spec']['persistentVolumeReclaimPolicy'] != 'Retain'):
                raise RuntimeError('Test retained PV identity changed')
            return obj if obj.get('status', {}).get('phase') == 'Released' else None
        wait_for(retained_pv)
        operations = [{'op': 'test', 'path': '/metadata/uid', 'value': pv_uid},
                      {'op': 'test', 'path': '/spec/claimRef/uid', 'value': old_pvc_uid},
                      {'op': 'test', 'path': '/spec/persistentVolumeReclaimPolicy', 'value': 'Retain'},
                      {'op': 'remove', 'path': '/spec/claimRef'}]
        response = client.patch(f'{base}/api/v1/persistentvolumes/{pv_name}', json=operations,
                                headers={'Content-Type': 'application/json-patch+json'})
        response.raise_for_status()
        def quota_released():
            response = client.get(f'{base}/api/v1/namespaces/{namespace}/resourcequotas/user-capacity')
            response.raise_for_status()
            return response.json().get('status', {}).get('used', {}).get('persistentvolumeclaims') == '0'
        wait_for(quota_released)
        replacement = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
                       'metadata': {'name': 'workspace-data', 'namespace': namespace,
                                    'labels': provider.objects['PersistentVolumeClaim']['metadata']['labels']},
                       'spec': {'storageClassName': 'codechat-workspace-retain', 'accessModes': ['ReadWriteOnce'],
                                'volumeName': pv_name, 'resources': {'requests': {'storage': '2Gi'}}}}
        response = client.post(f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims', json=replacement)
        response.raise_for_status()
        pvc_uid = response.json()['metadata']['uid']
        pvc_uids.add(pvc_uid)
        if pvc_uid == old_pvc_uid:
            raise RuntimeError('Replacement PVC did not get a new UID')
        wait_for(get_bound)
        try:
            observe_and_record_volume(sessions, provider, environment_id)
        except KubernetesProviderError:
            pass
        else:
            raise RuntimeError('Old binding incorrectly accepted a replacement PVC')
        pod_uid = create_pod(client)
        execute("assert open('/workspace/persistence-check.txt','rb').read()==b'codechat-test-data'")
        print('PASS: retained volume recovery restores data; old identity binding rejects replacement PVC')
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
                if current.json()['metadata'].get('labels', {}).get('codechat.io/qa-run') != qa_run:
                    raise RuntimeError('Test namespace marker missing; refusing cleanup')
                pod = client.get(f'{base}/api/v1/namespaces/{namespace}/pods/workspace-qa')
                if pod.status_code == 200:
                    if not pod_uid or pod.json()['metadata']['uid'] != pod_uid:
                        raise RuntimeError('Test pod outcome uncertain; preserve namespace for review: ' + namespace)
                    delete_pod(client, pod_uid)
                elif pod.status_code != 404:
                    raise RuntimeError('Unable to verify test pod before cleanup')
                claims = client.get(f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims')
                claims.raise_for_status()
                items = claims.json()['items']
                if items:
                    if len(items) != 1 or items[0]['metadata']['uid'] not in pvc_uids or not pv_uid or not volume:
                        raise RuntimeError('Test PVC outcome uncertain; preserve namespace for review: ' + namespace)
                    cleanup_pvc_uid = items[0]['metadata']['uid']
                    response = client.request('DELETE', f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims/workspace-data',
                                              json={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': cleanup_pvc_uid}})
                    if response.status_code not in (200, 202):
                        raise RuntimeError('Test PVC deletion failed')
                    wait_for(lambda: client.get(f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims/workspace-data').status_code == 404)
                if pv_uid and volume:
                    def safe_released_pv():
                        response = client.get(f'{base}/api/v1/persistentvolumes/{pv_name}')
                        response.raise_for_status()
                        obj = response.json()
                        ref = obj['spec'].get('claimRef')
                        if (obj['metadata']['uid'] != pv_uid or obj['metadata'].get('labels', {}).get('codechat.io/qa-run') != qa_run or
                                obj['spec']['csi']['volumeHandle'] != volume or
                                (ref and ref.get('uid') not in pvc_uids)):
                            raise RuntimeError('Test PV identity changed; refusing cleanup')
                        return obj if obj.get('status', {}).get('phase') == 'Released' else None
                    released = wait_for(safe_released_pv)
                    operations = [{'op': 'test', 'path': '/metadata/uid', 'value': pv_uid},
                                  {'op': 'test', 'path': '/spec/csi/volumeHandle', 'value': volume},
                                  {'op': 'test', 'path': '/spec/persistentVolumeReclaimPolicy', 'value': 'Retain'}]
                    if released['spec'].get('claimRef'):
                        operations.append({'op': 'test', 'path': '/spec/claimRef/uid', 'value': released['spec']['claimRef']['uid']})
                    operations.append({'op': 'replace', 'path': '/spec/persistentVolumeReclaimPolicy', 'value': 'Delete'})
                    response = client.patch(f'{base}/api/v1/persistentvolumes/{pv_name}', json=operations,
                                            headers={'Content-Type': 'application/json-patch+json'})
                    response.raise_for_status()
                    # Do not manually delete the PV: the CSI reclaim controller
                    # must first delete its backing Longhorn volume.
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
