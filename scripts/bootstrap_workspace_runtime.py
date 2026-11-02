"""Copy only the existing encryption key to the restricted workspace controller."""
import json
import os
import subprocess

command = [os.environ.get('KUBECTL_BIN', '/snap/bin/kubectl'), '--kubeconfig', os.environ['KUBECONFIG'],
           '--server', os.environ['PLATFORM_KUBE_SERVER'], '-n', 'platform-system']


def get(name):
    result = subprocess.check_output(command + ['get', 'secret', name, '--ignore-not-found', '-o', 'json'])
    return json.loads(result) if result.strip() else None


source = get('platform-model-provisioning')
if not source:
    raise RuntimeError('Existing model provisioning credentials are required')
field = 'PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY'
key = source['data'][field]
existing = get('platform-workspace-encryption')
if existing:
    if existing.get('data', {}).get(field) != key:
        raise RuntimeError('Workspace encryption identity differs; refusing automatic rotation')
else:
    document = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                'metadata': {'name': 'platform-workspace-encryption', 'namespace': 'platform-system'},
                'data': {field: key}}
    subprocess.run(command + ['create', '-f', '-'], input=json.dumps(document).encode(),
                   stdout=subprocess.DEVNULL, check=True)
print('Workspace encryption secret verified; no credential values printed or rotated.')
