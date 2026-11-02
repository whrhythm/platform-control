"""Run workspace file primitives against two disposable Kata/Longhorn volumes.

No browser/API endpoint, real user data or production environment is used.
All test resources are uniquely labelled and deleted after the test.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time

binary = os.environ.get('KUBECTL_BIN') or shutil.which('kubectl')
if not binary:
    raise RuntimeError('kubectl not found; set KUBECTL_BIN')
K = [binary, '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER']]
run = secrets.token_hex(6)
namespaces = {user: f'codechat-files-qa-{run}-{user}' for user in ('a', 'b')}
owner = {'codechat.io/qa-run': run, 'codechat.io/purpose': 'workspace-files-test'}
root = Path(__file__).resolve().parents[1]
source = (root / 'control_plane/workspace_files.py').read_text()
result_path = root / 'release/workspace-files-cluster-acceptance.json'
created = set()
volumes = set()
checks = []


def call(*args, stdin=None, timeout=90):
    completed = subprocess.run(K + list(args), input=stdin, text=True, capture_output=True, timeout=timeout)
    if completed.returncode:
        raise RuntimeError('kubectl ' + ' '.join(args[:3]) + ' failed: ' + completed.stderr[-1200:])
    return completed.stdout


def create(obj):
    data = json.dumps(obj)
    call('create', '--dry-run=server', '-f', '-', stdin=data)
    call('create', '-f', '-', stdin=data)


def wait_pod(ns, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pods = json.loads(call('-n', ns, 'get', 'pods', '-l', f'codechat.io/qa-run={run},codechat.io/component=runtime', '-o', 'json'))['items']
        if len(pods) == 1 and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pods[0].get('status', {}).get('conditions', [])):
            return pods[0]['metadata']['name']
        time.sleep(3)
    raise RuntimeError('Test sandbox did not become Ready')


def run_program(ns, pod, suffix):
    output = call('-n', ns, 'exec', '-i', pod, '--', 'python', '-', stdin=source + '\n' + suffix, timeout=90)
    if 'WORKSPACE_CHECK_OK' not in output:
        raise RuntimeError('Workspace check did not complete')


try:
    for user, ns in namespaces.items():
        labels = {**owner, 'codechat.io/qa-user': user}
        namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns, 'labels': labels}}
        create(namespace)
        created.add(ns)
        def meta(name):
            return {'name': name, 'namespace': ns, 'labels': labels}
        pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': meta('data'), 'spec': {'storageClassName': 'longhorn', 'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '1Gi'}}}}
        deny = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': meta('default-deny'), 'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}}
        sandbox = {'apiVersion': 'agents.x-k8s.io/v1beta1', 'kind': 'Sandbox', 'metadata': meta('workspace'), 'spec': {'service': False, 'shutdownPolicy': 'Retain', 'operatingMode': 'Running', 'podTemplate': {'metadata': {'labels': {**labels, 'codechat.io/component': 'runtime'}}, 'spec': {
            'runtimeClassName': 'kata-qemu-runtime-rs', 'restartPolicy': 'Never', 'automountServiceAccountToken': False,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [{'name': 'runtime', 'image': 'ghcr.nju.edu.cn/openhands/agent-canvas@sha256:638c5e3ce76df74344bb18bd85f44af395337ade8e71f04532099fb380febadf',
                            'command': ['python', '-c', 'import time; time.sleep(1800)'], 'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                            'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}},
                            'volumeMounts': [{'name': 'data', 'mountPath': '/workspace'}]}],
            'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'data'}}],
        }}}}
        for item in (pvc, deny, sandbox):
            create(item)
    pods = {u: wait_pod(ns) for u, ns in namespaces.items()}
    for user, ns in namespaces.items():
        pvc = json.loads(call('-n', ns, 'get', 'pvc', 'data', '-o', 'json'))
        assert pvc['status']['phase'] == 'Bound'
        volumes.add(pvc['spec']['volumeName'])
    assert len(volumes) == 2
    run_program(namespaces['a'], pods['a'], '''
f=WorkspaceFiles('/workspace', max_file_bytes=32)
f.create_directory('project')
f.write_file('project/source.py', [b'user-a'])
try:
    f.write_file('project/source.py', [b'x'*33], overwrite=True)
except UploadTooLarge:
    pass
else:
    raise AssertionError('oversized upload accepted')
assert f.read_file('project/source.py') == b'user-a'
os.link('/workspace/project/source.py', '/workspace/project/hardlink')
try:
    f.read_file('project/hardlink')
except InvalidWorkspacePath:
    pass
else:
    raise AssertionError('multi-linked file accepted')
os.unlink('/workspace/project/hardlink')
os.symlink('/etc', '/workspace/escape')
try:
    f.read_file('escape/passwd')
except OSError:
    pass
else:
    raise AssertionError('symlink escape accepted')
print('WORKSPACE_CHECK_OK')
''')
    run_program(namespaces['b'], pods['b'], '''
f=WorkspaceFiles('/workspace', max_file_bytes=32)
try:
    f.read_file('project/source.py')
except FileNotFoundError:
    pass
else:
    raise AssertionError('unexpected access to another test volume')
f.create_directory('project')
f.write_file('project/source.py', [b'user-b'])
assert f.read_file('project/source.py') == b'user-b'
print('WORKSPACE_CHECK_OK')
''')
    checks += ['separate_user_volumes', 'upload_read_and_limits', 'symlink_escape_denied', 'multi_linked_file_denied']
    old = pods['a']
    call('-n', namespaces['a'], 'patch', 'sandbox', 'workspace', '--type=merge', '-p', '{"spec":{"operatingMode":"Suspended"}}')
    call('-n', namespaces['a'], 'wait', '--for=delete', 'pod/' + old, '--timeout=120s', timeout=130)
    call('-n', namespaces['a'], 'patch', 'sandbox', 'workspace', '--type=merge', '-p', '{"spec":{"operatingMode":"Running"}}')
    resumed = wait_pod(namespaces['a'])
    run_program(namespaces['a'], resumed, "f=WorkspaceFiles('/workspace', max_file_bytes=32); assert f.read_file('project/source.py') == b'user-a'; print('WORKSPACE_CHECK_OK')")
    run_program(namespaces['b'], pods['b'], "f=WorkspaceFiles('/workspace', max_file_bytes=32); assert f.read_file('project/source.py') == b'user-b'; print('WORKSPACE_CHECK_OK')")
    checks += ['resume_preserves_files', 'other_user_unaffected']
    print('PASS:', ', '.join(checks), flush=True)
finally:
    result_path.parent.mkdir(exist_ok=True)
    result = {'run': run, 'checks': checks, 'cleanup': 'pending'}
    result_path.write_text(json.dumps(result, indent=2))
    for ns in sorted(created):
        obj = json.loads(call('get', 'namespace', ns, '-o', 'json'))
        if not ns.startswith(f'codechat-files-qa-{run}-') or any(obj['metadata'].get('labels', {}).get(k) != v for k, v in owner.items()):
            raise RuntimeError('Refusing deletion of a namespace without exact QA markers')
        call('delete', 'namespace', ns, '--wait=true', '--timeout=180s', timeout=200)
    result['cleanup'] = 'namespaces_deleted'
    deadline = time.monotonic() + 180
    while volumes and time.monotonic() < deadline:
        remaining = [v for v in volumes if call('get', 'pv', v, '--ignore-not-found', '-o', 'name').strip() or call('-n', 'longhorn-system', 'get', 'volumes.longhorn.io', v, '--ignore-not-found', '-o', 'name').strip()]
        if not remaining:
            result['cleanup'] = 'namespaces_pvs_and_longhorn_volumes_deleted'
            break
        time.sleep(5)
    else:
        if volumes:
            raise RuntimeError('Test backing volumes remain')
    result_path.write_text(json.dumps(result, indent=2))
    print('Cleaned owned workspace test resources', flush=True)
