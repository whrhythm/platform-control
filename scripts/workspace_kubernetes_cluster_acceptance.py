"""Scoped real-cluster adapter test: one synthetic namespace, no real PVC.

Cleanup requires the UID returned by the create response and uses a Kubernetes
delete precondition. If the response is lost, report the namespace for review.
"""
import os
import secrets
import shutil
import socket
import subprocess
import time

import httpx

from control_plane.kubernetes_workspace_provider import KubernetesWorkspaceProvider
from control_plane.workspace_provisioning import _metadata_match, _policy_match, _quota_match

binary = os.environ.get('KUBECTL_BIN') or shutil.which('kubectl')
if not binary:
    raise RuntimeError('kubectl not found')
subject = 'cluster-adapter-qa-' + secrets.token_hex(12)
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
base = f'http://127.0.0.1:{port}'
proxy = subprocess.Popen([binary, '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER'],
                          'proxy', '--address=127.0.0.1', '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
provider = dry = None
created_uid = None
namespace = None
try:
    with httpx.Client(timeout=1, trust_env=False) as client:
        for _ in range(40):
            try:
                if client.get(base + '/version').status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proxy.poll() is not None:
                raise RuntimeError('kubectl proxy exited')
            time.sleep(.25)
        else:
            raise RuntimeError('kubectl proxy not ready')
    options = dict(api_url=base, token='qa-proxy-placeholder', issuer='cluster-qa', subject=subject,
                   storage_gib=2, allow_test_loopback_http=True)
    provider = KubernetesWorkspaceProvider(**options)
    namespace = provider.namespace
    created = provider.create_namespace(provider.objects['Namespace'])
    created_uid = _metadata_match(created, provider.objects['Namespace'])
    observed = provider.read_namespace(namespace)
    _metadata_match(observed, provider.objects['Namespace'], namespace_uid=created_uid)
    policy = provider.create_network_policy(provider.objects['NetworkPolicy'])
    quota = provider.create_resource_quota(provider.objects['ResourceQuota'])
    policy_uid = _policy_match(policy, provider.objects['NetworkPolicy'])
    quota_uid = _quota_match(quota, provider.objects['ResourceQuota'])
    _policy_match(provider.read_network_policy(), provider.objects['NetworkPolicy'], policy_uid)
    _quota_match(provider.read_resource_quota(), provider.objects['ResourceQuota'], quota_uid)
    # ResourceQuota admission may require its controller to populate status.
    with httpx.Client(timeout=5, trust_env=False) as client:
        for _ in range(60):
            response = client.get(f'{base}/api/v1/namespaces/{namespace}/resourcequotas/user-capacity')
            if response.status_code == 200 and response.json().get('status', {}).get('hard'):
                break
            time.sleep(1)
        else:
            raise RuntimeError('ResourceQuota was not observed')
    dry = KubernetesWorkspaceProvider(**options, dry_run=True)
    pvc = dry.create_pvc(dry.objects['PersistentVolumeClaim'])
    assert pvc['spec']['storageClassName'] == 'codechat-workspace-retain'
    with httpx.Client(timeout=5, trust_env=False) as client:
        pvcs = client.get(f'{base}/api/v1/namespaces/{namespace}/persistentvolumeclaims')
        assert pvcs.status_code == 200 and not pvcs.json()['items']
    print('PASS: namespace, quota, default-deny and PVC server dry-run; no PVC persisted')
finally:
    if dry:
        dry.close()
    if provider:
        provider.close()
    try:
        if created_uid and namespace:
            with httpx.Client(timeout=15, trust_env=False) as client:
                response = client.request('DELETE', f'{base}/api/v1/namespaces/{namespace}', json={
                    'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': created_uid}})
                if response.status_code not in (200, 202):
                    raise RuntimeError(f'Test namespace cleanup failed: HTTP {response.status_code}; {namespace}')
                for _ in range(120):
                    result = client.get(f'{base}/api/v1/namespaces/{namespace}')
                    if result.status_code == 404:
                        break
                    if result.status_code != 200 or result.json().get('metadata', {}).get('uid') != created_uid:
                        raise RuntimeError('Test namespace identity changed; stop cleanup')
                    time.sleep(1)
                else:
                    raise RuntimeError('Test namespace deletion did not finish: ' + namespace)
            print('PASS: test namespace deleted with UID precondition')
        elif namespace:
            print('REVIEW: create outcome unknown for synthetic namespace:', namespace)
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait()
