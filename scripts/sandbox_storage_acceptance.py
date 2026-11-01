"""Disposable two-user Kata/Longhorn/Agent Sandbox acceptance.

Creates only uniquely labelled test namespaces, PVCs, sandbox CRs and a
negative-test Pod. Deletes only those namespaces after verifying ownership.
No real user identities, source code, credentials, or model calls are involved.
"""
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

K = ['/snap/bin/kubectl', '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER']]
run = secrets.token_hex(6)
namespaces = {user: f'codechat-qa-{run}-{user}' for user in ('a', 'b')}
owner = {'codechat.io/qa-run': run, 'codechat.io/purpose': 'sandbox-storage-test'}
created = set()
volumes = set()
checks = []
results = Path(__file__).resolve().parents[1] / 'release/sandbox-storage-acceptance.json'


def kubectl(*args, data=None, check=True, timeout=60):
    result = subprocess.run(K + list(args), input=json.dumps(data) if data is not None else None,
                            text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('kubectl ' + ' '.join(args[:3]) + ' failed: ' + result.stderr[-1200:])
    return result


def resource(kind, namespace, name):
    return json.loads(kubectl('-n', namespace, 'get', kind, name, '-o', 'json').stdout)


def pod_for(namespace):
    output = kubectl('-n', namespace, 'get', 'pods', '-l', f'codechat.io/qa-run={run},codechat.io/component=runtime', '-o', 'json').stdout
    pods = json.loads(output)['items']
    if len(pods) != 1:
        raise RuntimeError(f'Expected exactly one test runtime pod, got {len(pods)}')
    return pods[0]


def wait_for_pod(namespace, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        obj = kubectl('-n', namespace, 'get', 'pods', '-l', f'codechat.io/qa-run={run},codechat.io/component=runtime', '-o', 'json')
        pods = json.loads(obj.stdout)['items']
        if len(pods) == 1 and any(x.get('type') == 'Ready' and x.get('status') == 'True' for x in pods[0].get('status', {}).get('conditions', [])):
            return pods[0]
        time.sleep(3)
    raise RuntimeError('Timed out waiting for disposable sandbox pod to be Ready')


def manifest(user):
    ns = namespaces[user]
    labels = {**owner, 'codechat.io/qa-user': user}
    metadata = lambda name: {'name': name, 'namespace': ns, 'labels': labels}
    ns_obj = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns, 'labels': labels}}
    pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': metadata(f'data-{user}'),
           'spec': {'storageClassName': 'longhorn', 'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '1Gi'}}}}
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': metadata('default-deny'),
              'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}}
    script = f"if test -f /workspace/owner; then echo RESTORED; else printf '%s' 'user-{user}' > /workspace/owner; echo CREATED; fi; httpd -p 8080 -h /workspace; exec sleep 3600"
    sandbox = {'apiVersion': 'agents.x-k8s.io/v1beta1', 'kind': 'Sandbox', 'metadata': metadata('workspace'), 'spec': {
        'shutdownPolicy': 'Retain', 'operatingMode': 'Running', 'service': False,
        'podTemplate': {'metadata': {'labels': {**labels, 'codechat.io/component': 'runtime'}}, 'spec': {
            'runtimeClassName': 'kata-qemu-runtime-rs', 'restartPolicy': 'Never', 'automountServiceAccountToken': False,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 65534, 'runAsGroup': 65534, 'fsGroup': 65534, 'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [{'name': 'runtime', 'image': 'harbor.k3s.local/internal/busybox:1.36', 'command': ['sh', '-c', script],
                            'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                            'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}},
                            'volumeMounts': [{'name': 'data', 'mountPath': '/workspace'}]}],
            'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': f'data-{user}'}}],
        }}},
    }
    return ns_obj, pvc, policy, sandbox


try:
    for user, ns in namespaces.items():
        ns_obj, pvc, policy, sandbox = manifest(user)
        kubectl('create', '--dry-run=server', '-f', '-', data=ns_obj)
        kubectl('create', '-f', '-', data=ns_obj)
        created.add(ns)
        for obj in (pvc, policy, sandbox):
            kubectl('create', '--dry-run=server', '-f', '-', data=obj)
            kubectl('create', '-f', '-', data=obj)
    pods = {user: wait_for_pod(ns) for user, ns in namespaces.items()}
    for user, ns in namespaces.items():
        pod = pods[user]
        assert pod['spec']['runtimeClassName'] == 'kata-qemu-runtime-rs'
        pvc = resource('pvc', ns, f'data-{user}')
        assert pvc['status']['phase'] == 'Bound'
        volumes.add(pvc['spec']['volumeName'])
        name = pod['metadata']['name']
        output = kubectl('-n', ns, 'exec', name, '--', 'cat', '/workspace/owner').stdout.strip()
        assert output == f'user-{user}'
        own_http = kubectl('-n', ns, 'exec', name, '--', 'wget', '-q', '-T', '5', '-O', '-', 'http://127.0.0.1:8080/owner').stdout.strip()
        assert own_http == f'user-{user}'
    checks += ['two_kata_sandboxes_ready', 'separate_bound_volumes_and_files', 'local_http_probe']
    # The remote shell must run successfully; an exec failure cannot count as isolation.
    for source, target in (('a', 'b'), ('b', 'a')):
        target_ip = pods[target]['status']['podIP']
        command = f"if wget -q -T 5 -O - http://{target_ip}:8080/owner >/dev/null; then echo REACHABLE; else echo BLOCKED; fi"
        probe = kubectl('-n', namespaces[source], 'exec', pods[source]['metadata']['name'], '--', 'sh', '-c', command, timeout=20)
        if probe.stdout.strip() != 'BLOCKED':
            raise RuntimeError('Cross-user network request unexpectedly succeeded')
    checks.append('cross_user_network_blocked')
    old_name = pods['a']['metadata']['name']
    kubectl('-n', namespaces['a'], 'patch', 'sandbox', 'workspace', '--type=merge', '-p', '{"spec":{"operatingMode":"Suspended"}}')
    kubectl('-n', namespaces['a'], 'wait', '--for=delete', f'pod/{old_name}', '--timeout=120s', timeout=130)
    assert resource('pvc', namespaces['a'], 'data-a')['status']['phase'] == 'Bound'
    kubectl('-n', namespaces['a'], 'patch', 'sandbox', 'workspace', '--type=merge', '-p', '{"spec":{"operatingMode":"Running"}}')
    resumed = wait_for_pod(namespaces['a'])
    assert kubectl('-n', namespaces['a'], 'exec', resumed['metadata']['name'], '--', 'cat', '/workspace/owner').stdout.strip() == 'user-a'
    assert kubectl('-n', namespaces['b'], 'exec', pods['b']['metadata']['name'], '--', 'cat', '/workspace/owner').stdout.strip() == 'user-b'
    checks += ['suspend_preserves_pvc', 'resume_restores_files', 'other_user_unaffected']
    print('PASS:', ', '.join(checks), flush=True)
finally:
    result = {'run': run, 'checks': checks, 'cleanup': 'pending'}
    results.parent.mkdir(exist_ok=True)
    results.write_text(json.dumps(result, indent=2))
    for ns in sorted(created):
        obj = resource('namespace', ns, ns)
        if not ns.startswith(f'codechat-qa-{run}-') or any(obj['metadata'].get('labels', {}).get(k) != v for k, v in owner.items()):
            raise RuntimeError('Refusing to delete namespace without exact QA ownership markers')
        kubectl('delete', 'namespace', ns, '--wait=true', '--timeout=180s', timeout=200)
    result['cleanup'] = 'namespaces_deleted'
    if volumes:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            remaining_pvs = [name for name in volumes if kubectl('get', 'pv', name, '--ignore-not-found', '-o', 'name').stdout.strip()]
            remaining_volumes = [name for name in volumes if kubectl('-n', 'longhorn-system', 'get', 'volumes.longhorn.io', name, '--ignore-not-found', '-o', 'name').stdout.strip()]
            if not remaining_pvs and not remaining_volumes:
                result['cleanup'] = 'namespaces_pvs_and_longhorn_volumes_deleted'
                break
            time.sleep(5)
        else:
            raise RuntimeError('Test namespace deletion completed but backing volume cleanup remains pending')
    results.write_text(json.dumps(result, indent=2))
    print('Cleaned owned disposable test namespaces', flush=True)
