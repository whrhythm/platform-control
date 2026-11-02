"""Verify Kata-to-Kata Calico policy using two disposable namespaces only."""
import json
import os
import secrets
import shutil
import subprocess
import time

binary = os.environ.get('KUBECTL_BIN') or shutil.which('kubectl')
if not binary:
    raise RuntimeError('kubectl not found')
k = [binary, '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER']]
run = secrets.token_hex(6)
owner = {'codechat.io/qa-run': run, 'codechat.io/purpose': 'workspace-network-test'}
namespaces = {s: 'codechat-net-qa-' + run + '-' + s for s in ('source', 'target')}
created = {}
image = 'ghcr.nju.edu.cn/openhands/agent-canvas@sha256:638c5e3ce76df74344bb18bd85f44af395337ade8e71f04532099fb380febadf'


def call(*args, obj=None, check=True):
    result = subprocess.run(k + list(args), input=json.dumps(obj) if obj else None, text=True, capture_output=True, timeout=180)
    if check and result.returncode:
        raise RuntimeError('kubectl failed: ' + result.stderr[-500:])
    return result


def create(obj):
    return json.loads(call('create', '-f', '-', '-o', 'json', obj=obj).stdout)


def wait_until(fn, seconds=120):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if fn():
            return
        time.sleep(3)
    raise RuntimeError('Timed out waiting for network test condition')


def probe(host):
    program = "import socket,sys; s=socket.socket(); s.settimeout(2); r=s.connect_ex((sys.argv[1],18080)); s.close(); sys.exit(0 if r==0 else 1)"
    return call('-n', namespaces['source'], 'exec', 'probe', '--', 'python', '-c', program, host, check=False).returncode == 0


def policy(namespace, direction):
    return {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': 'qa-deny', 'namespace': namespace, 'labels': owner},
            'spec': {'podSelector': {}, 'policyTypes': [direction]}}


try:
    for which, ns in namespaces.items():
        created_ns = create({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns, 'labels': owner}})
        created[ns] = created_ns['metadata']['uid']
        command = ['python', '-m', 'http.server', '18080', '--bind', '0.0.0.0'] if which == 'target' else ['python', '-c', 'import time; time.sleep(900)']
        create({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'probe', 'namespace': ns, 'labels': owner},
                'spec': {'runtimeClassName': 'kata-qemu-runtime-rs', 'automountServiceAccountToken': False,
                         'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
                         'containers': [{'name': 'probe', 'image': image, 'command': command,
                                         'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                                         'resources': {'requests': {'cpu': '100m', 'memory': '192Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}}}]}})
    for ns in namespaces.values():
        call('-n', ns, 'wait', '--for=condition=Ready', 'pod/probe', '--timeout=180s')
    target = json.loads(call('-n', namespaces['target'], 'get', 'pod', 'probe', '-o', 'json').stdout)['status']['podIP']
    wait_until(lambda: probe(target))
    print('PASS: baseline Kata-to-Kata connection')
    create(policy(namespaces['target'], 'Ingress'))
    wait_until(lambda: not probe(target))
    # The target must still listen locally; a dead server is not evidence of isolation.
    local = call('-n', namespaces['target'], 'exec', 'probe', '--', 'python', '-c',
                 "import socket; socket.create_connection(('127.0.0.1',18080),2).close()", check=False)
    if local.returncode:
        raise RuntimeError('Target stopped listening during ingress test')
    print('PASS: target ingress deny blocks the connection')
    existing = json.loads(call('-n', namespaces['target'], 'get', 'networkpolicy', 'qa-deny', '-o', 'json').stdout)
    if any(existing['metadata'].get('labels', {}).get(key) != value for key, value in owner.items()):
        raise RuntimeError('Test policy ownership changed')
    call('delete', '--raw', '/apis/networking.k8s.io/v1/namespaces/' + namespaces['target'] + '/networkpolicies/qa-deny', '-f', '-',
         obj={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': existing['metadata']['uid']}})
    wait_until(lambda: probe(target))
    create(policy(namespaces['source'], 'Egress'))
    wait_until(lambda: not probe(target))
    print('PASS: source egress deny blocks the connection')
finally:
    for ns, uid in created.items():
        existing = json.loads(call('get', 'namespace', ns, '-o', 'json').stdout)
        if existing['metadata']['uid'] != uid or any(existing['metadata'].get('labels', {}).get(key) != value for key, value in owner.items()):
            raise RuntimeError('Test namespace ownership changed; refusing cleanup: ' + ns)
        # Kubernetes API DELETE with a UID precondition; kubectl replace --raw supplies JSON.
        payload = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}}
        call('delete', '--raw', '/api/v1/namespaces/' + ns, '-f', '-', obj=payload)
        wait_until(lambda: not call('get', 'namespace', ns, '--ignore-not-found', '-o', 'name').stdout.strip(), seconds=180)
    print('Cleanup completed for owned test namespaces')
