"""Run a sealed non-root storage bootstrap via the already built backend image."""
import base64
import json
import os
import secrets
import shlex

from cluster_transport import remote


def get_secret(namespace, name):
    value = remote(['kubectl', '-n', namespace, 'get', 'secret', name, '--ignore-not-found', '-o', 'json'])
    return json.loads(value) if value.strip() else None


def main():
    image = os.environ['PLATFORM_STORAGE_BOOTSTRAP_IMAGE']
    root = get_secret('minio', 'minio-root')
    existing = get_secret('platform-system', 'platform-object-storage-provisioner')
    if existing:
        key = base64.b64decode(existing['data']['PLATFORM_OBJECT_STORAGE_ACCESS_KEY']).decode()
        secret = base64.b64decode(existing['data']['PLATFORM_OBJECT_STORAGE_SECRET_KEY']).decode()
        if key != 'codechat-s3-provider':
            raise RuntimeError('Unexpected storage identity; no credentials changed')
    else:
        key, secret = 'codechat-s3-provider', secrets.token_urlsafe(30)
        remote(['kubectl', 'create', '-f', '-'], json.dumps({'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
            'metadata': {'name': 'platform-object-storage-provisioner', 'namespace': 'platform-system'},
            'stringData': {'PLATFORM_OBJECT_STORAGE_ACCESS_KEY': key, 'PLATFORM_OBJECT_STORAGE_SECRET_KEY': secret}}), redact=(secret,))
    payload = {'endpoint': '192.168.10.30:19000', 'access_key': key, 'secret_key': secret,
        'root_access': base64.b64decode(root['data']['root-user']).decode(),
        'root_secret': base64.b64decode(root['data']['root-password']).decode()}
    # A bounded host-only tunnel; no S3/console ingress or service exposure is added.
    command = "kubectl -n minio port-forward --address=127.0.0.1 svc/minio 19000:9000 >/tmp/codechat-storage-forward.log 2>&1 & forward_pid=$!; trap 'kill $forward_pid 2>/dev/null || true' EXIT; sleep 2; kill -0 $forward_pid || exit 1; docker run --rm -i --network=host " + shlex.quote(image) + " python -m control_plane.object_storage_bootstrap"
    payload['endpoint'] = '127.0.0.1:19000'
    output = remote(['/bin/sh', '-c', command], json.dumps(payload), redact=(secret, payload['root_secret'], payload['root_access']))
    print('\n'.join(line for line in output.splitlines() if line.startswith('RESULT:')))


if __name__ == '__main__':
    main()
