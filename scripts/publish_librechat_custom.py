"""Run on edge; publish the built CodeChat LibreChat image without persisting credentials."""
import base64
import json
import os
import subprocess
import tempfile

TAG = os.environ.get('CODECHAT_IMAGE_TAG', '0.8.9-codechat-20261031.1')
SOURCE = 'codechat-librechat:' + TAG
TARGET = 'harbor.k3s.local/internal/codechat-librechat:' + TAG

secret = json.loads(subprocess.check_output(['kubectl', '-n', 'harbor', 'get', 'secret', 'harbor-secrets', '-o', 'json'], text=True))
password = base64.b64decode(secret['data']['HARBOR_ADMIN_PASSWORD']).decode()
with tempfile.TemporaryDirectory(prefix='codechat-registry-') as config:
    os.chmod(config, 0o700)
    env = {**os.environ, 'DOCKER_CONFIG': config}
    login = subprocess.run(['docker', 'login', 'harbor.k3s.local', '--username', 'admin', '--password-stdin'], input=password, text=True, capture_output=True, env=env)
    if login.returncode:
        raise RuntimeError('Harbor login failed')
    subprocess.run(['docker', 'tag', SOURCE, TARGET], check=True)
    push = subprocess.run(['docker', 'push', TARGET], capture_output=True, text=True, env=env)
    if push.returncode:
        raise RuntimeError('Harbor push failed: ' + push.stderr.replace(password, '[redacted]')[-1200:])
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', TARGET], text=True))[0]
    print(json.dumps({'image': TARGET, 'repo_digests': image['RepoDigests']}))
