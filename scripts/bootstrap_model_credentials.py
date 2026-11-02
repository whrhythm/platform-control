"""Create provisioning secrets once, without printing or committing their values."""
import base64
import json
import os
import secrets
import shutil
import subprocess
from cryptography.fernet import Fernet

kubectl_bin = os.environ.get('KUBECTL_BIN') or shutil.which('kubectl')
if not kubectl_bin:
    raise RuntimeError('kubectl not found; set KUBECTL_BIN to its absolute path')
kubectl = [kubectl_bin, '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER']]


def get(namespace, name):
    result = subprocess.run(kubectl + ['-n', namespace, 'get', 'secret', name, '-o', 'json', '--ignore-not-found'], capture_output=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def value(secret, name):
    return base64.b64decode(secret['data'][name]).decode()


def create(namespace, name, values):
    obj = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': namespace}, 'type': 'Opaque', 'stringData': values}
    subprocess.run(kubectl + ['create', '-f', '-'], input=json.dumps(obj).encode(), stdout=subprocess.DEVNULL, check=True)


admin = get('platform-system', 'platform-litellm-admin')
if not admin:
    raise RuntimeError('Existing platform-litellm-admin secret is missing')
data_keys = list(admin.get('data', {}))
if len(data_keys) != 1:
    raise RuntimeError('Expected exactly one LiteLLM admin secret field')
master = value(admin, data_keys[0])
platform = get('platform-system', 'platform-model-provisioning')
chat = get('librechat', 'librechat-codechat-provisioning')
if not platform and chat:
    raise RuntimeError('Inconsistent secrets: restore the platform secret from backup; do not rotate automatically')
if not platform:
    create('platform-system', 'platform-model-provisioning', {
        'PLATFORM_LITELLM_MASTER_KEY': master,
        'PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY': Fernet.generate_key().decode(),
        'PLATFORM_LIBRECHAT_SERVICE_TOKEN': secrets.token_urlsafe(48),
    })
    platform = get('platform-system', 'platform-model-provisioning')
if value(platform, 'PLATFORM_LITELLM_MASTER_KEY') != master:
    raise RuntimeError('LiteLLM admin key differs from existing provisioning secret; explicit rotation is required')
service_token = value(platform, 'PLATFORM_LIBRECHAT_SERVICE_TOKEN')
if not chat:
    create('librechat', 'librechat-codechat-provisioning', {'CODECHAT_SERVICE_TOKEN': service_token})
elif not secrets.compare_digest(value(chat, 'CODECHAT_SERVICE_TOKEN'), service_token):
    raise RuntimeError('LibreChat and platform service credentials differ; explicit rotation is required')
print('Provisioning secrets exist and match; no existing secret was overwritten.')
