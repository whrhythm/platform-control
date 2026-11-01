"""Exercise the installed OpenHands SDK against a disposable Kata Agent Server.

The existing Canvas image is reused solely as a source of the matching server
binary; its all-in-one entrypoint/UI is not started. Test credentials are random
and never printed. This test does not call any LLM or touch user data.
"""
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

K = ['/snap/bin/kubectl', '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER']]
run = secrets.token_hex(6)
ns = f'codechat-agent-qa-{run}'
labels = {'codechat.io/qa-run': run, 'codechat.io/purpose': 'agent-server-test'}
image = 'ghcr.nju.edu.cn/openhands/agent-canvas@sha256:638c5e3ce76df74344bb18bd85f44af395337ade8e71f04532099fb380febadf'
created = False
volume = None
checks = []
out = Path(__file__).resolve().parents[1] / 'release/agent-server-acceptance.json'


def call(*args, data=None, timeout=90):
    r = subprocess.run(K + list(args), input=json.dumps(data) if data is not None else None, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError('Cluster operation failed: ' + ' '.join(args[:3]) + ': ' + r.stderr[-1000:])
    return r.stdout


def ready(selector, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pods = json.loads(call('-n', ns, 'get', 'pods', '-l', selector, '-o', 'json'))['items']
        if len(pods) == 1 and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pods[0].get('status', {}).get('conditions', [])):
            return pods[0]
        time.sleep(4)
    raise RuntimeError('Test pod was not Ready before deadline')


def metadata(name):
    return {'name': name, 'namespace': ns, 'labels': labels}


try:
    namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns, 'labels': labels}}
    call('create', '--dry-run=server', '-f', '-', data=namespace)
    call('create', '-f', '-', data=namespace)
    created = True
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': metadata('session'), 'type': 'Opaque',
              'stringData': {'SESSION_API_KEY': secrets.token_urlsafe(40), 'OH_SECRET_KEY': secrets.token_urlsafe(40)}}
    pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': metadata('data'), 'spec': {'storageClassName': 'longhorn', 'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '1Gi'}}}}
    deny = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': metadata('default-deny'), 'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}}
    allow = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': metadata('test-client-to-server'), 'spec': {'podSelector': {'matchLabels': {'codechat.io/component': 'server'}}, 'policyTypes': ['Ingress'], 'ingress': [{'from': [{'podSelector': {'matchLabels': {'codechat.io/component': 'client'}}}], 'ports': [{'protocol': 'TCP', 'port': 8000}]}]}}
    egress = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': metadata('client-egress'), 'spec': {'podSelector': {'matchLabels': {'codechat.io/component': 'client'}}, 'policyTypes': ['Egress'], 'egress': [{'to': [{'podSelector': {'matchLabels': {'codechat.io/component': 'server'}}}], 'ports': [{'protocol': 'TCP', 'port': 8000}]}]}}
    common_env = [{'name': 'OPENHANDS_SUPPRESS_BANNER', 'value': '1'}, {'name': 'LITELLM_LOCAL_MODEL_COST_MAP', 'value': 'True'}]
    common_security = {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}}
    container_security = {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}}
    server_container = {'name': 'agent-server', 'image': image, 'workingDir': '/home/openhands', 'command': ['python', '-m', 'openhands.agent_server', '--host', '0.0.0.0', '--port', '8000'], 'env': common_env, 'envFrom': [{'secretRef': {'name': 'session'}}], 'securityContext': container_security, 'resources': {'requests': {'cpu': '250m', 'memory': '768Mi'}, 'limits': {'cpu': '2', 'memory': '2Gi'}}, 'readinessProbe': {'httpGet': {'path': '/health', 'port': 8000}, 'periodSeconds': 5, 'failureThreshold': 30}, 'volumeMounts': [{'name': 'data', 'mountPath': '/home/openhands/workspace', 'subPath': 'workspace'}, {'name': 'data', 'mountPath': '/home/openhands/.openhands', 'subPath': 'openhands'}]}
    sandbox = {'apiVersion': 'agents.x-k8s.io/v1beta1', 'kind': 'Sandbox', 'metadata': metadata('runtime'), 'spec': {'service': False, 'shutdownPolicy': 'Retain', 'podTemplate': {'metadata': {'labels': {**labels, 'codechat.io/component': 'server'}}, 'spec': {'runtimeClassName': 'kata-qemu-runtime-rs', 'restartPolicy': 'Never', 'automountServiceAccountToken': False, 'securityContext': common_security, 'containers': [server_container], 'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'data'}}]}}}}
    client = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'client', 'namespace': ns, 'labels': {**labels, 'codechat.io/component': 'client'}}, 'spec': {'automountServiceAccountToken': False, 'securityContext': common_security, 'containers': [{'name': 'client', 'image': image, 'command': ['sh', '-c', 'exec sleep 1800'], 'env': common_env, 'envFrom': [{'secretRef': {'name': 'session'}}], 'securityContext': container_security, 'resources': {'requests': {'cpu': '100m', 'memory': '512Mi'}, 'limits': {'cpu': '1', 'memory': '1Gi'}}}]}}
    for obj in (secret, pvc, deny, allow, egress, sandbox, client):
        call('create', '--dry-run=server', '-f', '-', data=obj)
        call('create', '-f', '-', data=obj)
    server = ready('codechat.io/component=server')
    ready('codechat.io/component=client')
    pvc_info = json.loads(call('-n', ns, 'get', 'pvc', 'data', '-o', 'json'))
    volume = pvc_info['spec']['volumeName']
    ip = server['status']['podIP']
    script = '''
import os,sys,urllib.request,urllib.error
from openhands.sdk import RemoteWorkspace
url='http://'+sys.argv[1]+':8000'
try:
    urllib.request.urlopen(url+'/api/conversations/search', timeout=5)
except urllib.error.HTTPError as e:
    assert e.code in (401,403)
else:
    raise RuntimeError('Unauthenticated request was accepted')
ws=RemoteWorkspace(host=url, working_dir='/home/openhands/workspace', api_key=os.environ['SESSION_API_KEY'])
assert ws.get_server_info()
result=ws.execute_command("printf codechat-test > /home/openhands/workspace/probe && cat /home/openhands/workspace/probe")
assert 'codechat-test' in str(result)
print('AUTH_AND_REMOTE_EXEC_OK')
'''
    output = call('-n', ns, 'exec', 'client', '--', 'python', '-c', script, ip, timeout=120)
    if 'AUTH_AND_REMOTE_EXEC_OK' not in output:
        raise RuntimeError('SDK probe did not confirm authenticated execution')
    checks += ['kata_agent_server_ready', 'unauthenticated_denied', 'matching_sdk_remote_execution', 'user_volume_write']
    print('PASS:', ', '.join(checks), flush=True)
finally:
    result = {'run': run, 'checks': checks, 'cleanup': 'pending'}
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    if created:
        obj = json.loads(call('get', 'namespace', ns, '-o', 'json'))
        if ns != f'codechat-agent-qa-{run}' or any(obj['metadata'].get('labels', {}).get(k) != v for k, v in labels.items()):
            raise RuntimeError('Refusing cleanup of namespace without exact test ownership')
        call('delete', 'namespace', ns, '--wait=true', '--timeout=180s', timeout=200)
        result['cleanup'] = 'namespace_deleted'
        if volume:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                pv = call('get', 'pv', volume, '--ignore-not-found', '-o', 'name').strip()
                lh = call('-n', 'longhorn-system', 'get', 'volumes.longhorn.io', volume, '--ignore-not-found', '-o', 'name').strip()
                if not pv and not lh:
                    result['cleanup'] = 'namespace_pv_and_longhorn_volume_deleted'
                    break
                time.sleep(5)
            else:
                raise RuntimeError('Test backing volume cleanup remains pending')
    out.write_text(json.dumps(result, indent=2))
    print('Cleaned owned Agent Server test resources', flush=True)
