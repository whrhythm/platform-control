"""Disposable two-user teardown acceptance: deleting user A keeps user B usable.

Exercises the documented deletion order: stop compute (suspend and confirm the
Pod exited), remove the sandbox, delete the PVC, then verify the backing PV and
Longhorn volume are really reclaimed before user B is re-checked end to end.
Creates only uniquely labelled test namespaces, PVCs and sandbox CRs. Deletes
only those namespaces after verifying ownership. No real user identities,
source code, credentials, or model calls are involved.
Requires KUBECONFIG and PLATFORM_KUBE_SERVER (a full URL with scheme, e.g. https://127.0.0.1:6443).
"""
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

K = [os.environ.get('KUBECTL_BIN', '/usr/bin/kubectl'), '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER']]
run = secrets.token_hex(6)
namespaces = {user: f'codechat-qa-{run}-{user}' for user in ('a', 'b')}
owner = {'codechat.io/qa-run': run, 'codechat.io/purpose': 'sandbox-teardown-test'}
created = set()
volumes = {'a': None, 'b': None}
checks = []
results = Path(__file__).resolve().parents[1] / 'release/sandbox-teardown-acceptance.json'


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


def wait_gone(scope, kind, name, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        args = (['-n', scope] if scope else []) + ['get', kind, name, '--ignore-not-found', '-o', 'name']
        if not kubectl(*args).stdout.strip():
            return
        time.sleep(5)
    raise RuntimeError(f'{kind}/{name} still present after teardown deadline')


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


def verify_usable(user):
    ns = namespaces[user]
    pod = pod_for(ns)
    assert pod['spec']['runtimeClassName'] == 'kata-qemu-runtime-rs'
    assert resource('pvc', ns, f'data-{user}')['status']['phase'] == 'Bound'
    name = pod['metadata']['name']
    assert kubectl('-n', ns, 'exec', name, '--', 'cat', '/workspace/owner').stdout.strip() == f'user-{user}'
    probe = kubectl('-n', ns, 'exec', name, '--', 'wget', '-q', '-T', '5', '-O', '-', 'http://127.0.0.1:8080/owner').stdout.strip()
    assert probe == f'user-{user}'


try:
    for user, ns in namespaces.items():
        ns_obj, pvc, policy, sandbox = manifest(user)
        kubectl('create', '--dry-run=server', '-f', '-', data=ns_obj)
        kubectl('create', '-f', '-', data=ns_obj)
        created.add(ns)
        for obj in (pvc, policy, sandbox):
            kubectl('create', '--dry-run=server', '-f', '-', data=obj)
            kubectl('create', '-f', '-', data=obj)
    wait_for_pod(namespaces['a'])
    wait_for_pod(namespaces['b'])
    verify_usable('a')
    verify_usable('b')
    volumes['a'] = resource('pvc', namespaces['a'], 'data-a')['spec']['volumeName']
    volumes['b'] = resource('pvc', namespaces['b'], 'data-b')['spec']['volumeName']
    assert volumes['a'] != volumes['b']
    checks += ['two_kata_sandboxes_ready', 'separate_bound_volumes_and_files']
    ns = namespaces['a']
    old_name = pod_for(ns)['metadata']['name']
    kubectl('-n', ns, 'patch', 'sandbox', 'workspace', '--type=merge', '-p', '{"spec":{"operatingMode":"Suspended"}}')
    kubectl('-n', ns, 'wait', '--for=delete', f'pod/{old_name}', '--timeout=120s', timeout=130)
    checks.append('user_a_compute_stopped')
    kubectl('-n', ns, 'delete', 'pvc', 'data-a', '--wait=true', '--timeout=120s', timeout=130)
    kubectl('-n', ns, 'delete', 'namespace', ns, '--wait=true', '--timeout=180s', timeout=200)
    wait_gone(None, 'pv', volumes['a'])
    wait_gone('longhorn-system', 'volumes.longhorn.io', volumes['a'])
    checks += ['user_a_environment_deleted', 'user_a_volume_reclaimed']
    verify_usable('b')
    checks.append('user_b_still_usable_after_a_deleted')
    print('PASS:', ', '.join(checks), flush=True)
finally:
    result = {'run': run, 'checks': checks, 'cleanup': 'pending'}
    results.parent.mkdir(exist_ok=True)
    results.write_text(json.dumps(result, indent=2))
    for ns in sorted(created):
        if not ns.startswith(f'codechat-qa-{run}-'):
            raise RuntimeError('Refusing to delete namespace without the exact QA run prefix')
        if kubectl('get', 'namespace', ns, '--ignore-not-found', '-o', 'name').stdout.strip():
            obj = resource('namespace', ns, ns)
            if any(obj['metadata'].get('labels', {}).get(k) != v for k, v in owner.items()):
                raise RuntimeError('Refusing to delete namespace without exact QA ownership markers')
            kubectl('delete', 'namespace', ns, '--wait=true', '--timeout=180s', timeout=200)
    result['cleanup'] = 'namespaces_deleted'
    for user, volume in volumes.items():
        if volume:
            wait_gone(None, 'pv', volume)
            wait_gone('longhorn-system', 'volumes.longhorn.io', volume)
    result['cleanup'] = 'namespaces_pvs_and_longhorn_volumes_deleted'
    results.write_text(json.dumps(result, indent=2))
    print('Cleaned owned disposable test namespaces', flush=True)
