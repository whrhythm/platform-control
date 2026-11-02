"""Create/delete a header-routed preview isolated from the production Service."""
import json
import os
import shutil
import subprocess
import sys

kubectl_bin = os.environ.get('KUBECTL_BIN') or shutil.which('kubectl')
if not kubectl_bin:
    raise RuntimeError('kubectl not found; set KUBECTL_BIN to its absolute path')
K = [kubectl_bin, '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER'], '-n', 'librechat']
name = 'codechat-preview'
resources = [('deployment', name), ('service', name), ('networkpolicy', name), ('networkpolicy', name + '-mongo'), ('apisixroute', name)]


def get(kind, target):
    result = subprocess.run(K + ['get', kind, target, '-o', 'json', '--ignore-not-found'], capture_output=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


if sys.argv[1] == 'cleanup':
    for kind, target in reversed(resources):
        obj = get(kind, target)
        if obj and obj['metadata'].get('labels', {}).get('codechat-preview-owned') != 'true':
            raise RuntimeError('Refusing to delete resource without preview ownership label')
        if obj:
            subprocess.run(K + ['delete', kind, target, '--wait=false'], check=True, stdout=subprocess.DEVNULL)
    print('Owned preview resources removed')
    raise SystemExit

if sys.argv[1] != 'create':
    raise SystemExit('Use create or cleanup')
if any(get(kind, target) for kind, target in resources):
    raise RuntimeError('A preview resource already exists; refusing to overwrite')
header = os.environ['CODECHAT_PREVIEW_HEADER']
image = os.environ['CODECHAT_PREVIEW_IMAGE']
if len(header) < 32 or not image.startswith('harbor.k3s.local/internal/codechat-librechat@sha256:'):
    raise RuntimeError('Invalid preview configuration')
owner = {'codechat-preview-owned': 'true'}
dep = get('deployment', 'librechat')
dep['metadata'] = {'name': name, 'namespace': 'librechat', 'labels': owner}
dep.pop('status', None)
dep['spec']['selector'] = {'matchLabels': {'app': name}}
dep['spec']['template']['metadata']['labels'] = {'app': name}
pod = dep['spec']['template']['spec']
pod['hostAliases'] = [{'ip': '192.168.10.30', 'hostnames': ['authentik.k3s.local', 'platform.k3s.local']}]
container = pod['containers'][0]
container['image'] = image
container.setdefault('env', []).append({'name': 'CODECHAT_PROVISION_URL', 'value': 'https://platform.k3s.local:30443/internal/v1/litellm/key'})
container['envFrom'].append({'secretRef': {'name': 'librechat-codechat-provisioning'}})
for volume in pod['volumes']:
    if volume['name'] == 'files':
        volume.pop('persistentVolumeClaim', None)
        volume['emptyDir'] = {'sizeLimit': '1Gi'}
service = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': name, 'namespace': 'librechat', 'labels': owner}, 'spec': {'selector': {'app': name}, 'ports': [{'port': 3080, 'targetPort': 3080}]}}
policy = get('networkpolicy', 'librechat-app')
policy['metadata'] = {'name': name, 'namespace': 'librechat', 'labels': owner}
policy['spec']['podSelector'] = {'matchLabels': {'app': name}}
mongo = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': {'name': name + '-mongo', 'namespace': 'librechat', 'labels': owner}, 'spec': {'podSelector': {'matchLabels': {'app': 'librechat-mongo'}}, 'policyTypes': ['Ingress'], 'ingress': [{'from': [{'podSelector': {'matchLabels': {'app': name}}}], 'ports': [{'protocol': 'TCP', 'port': 27017}]}]}}
route = {'apiVersion': 'apisix.apache.org/v2', 'kind': 'ApisixRoute', 'metadata': {'name': name, 'namespace': 'librechat', 'labels': owner}, 'spec': {'ingressClassName': 'apisix', 'http': [{'name': name, 'priority': 100, 'match': {'hosts': ['chat.k3s.local'], 'paths': ['/*'], 'exprs': [{'subject': {'scope': 'Header', 'name': 'X-CodeChat-Preview'}, 'op': 'Equal', 'value': header}]}, 'backends': [{'serviceName': name, 'servicePort': 3080}]}]}}
data = json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': [dep, service, policy, mongo, route]}).encode()
subprocess.run(K + ['create', '--dry-run=server', '-f', '-'], input=data, check=True, stdout=subprocess.DEVNULL)
subprocess.run(K + ['create', '-f', '-'], input=data, check=True, stdout=subprocess.DEVNULL)
print('Preview resources created; ordinary LibreChat Service selector unchanged')
