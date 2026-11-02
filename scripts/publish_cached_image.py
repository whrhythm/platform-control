"""Publish a named operator-owned local image; transient registry login only."""
import argparse
import base64
import json
import os
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('source')
parser.add_argument('target')
parser.add_argument('--publisher', action='store_true')
args = parser.parse_args()
if not re.fullmatch(r'harbor\.k3s\.local/(internal|codechat-base)/[a-z0-9._-]+:[A-Za-z0-9._-]+', args.target):
    raise ValueError('Only the internal release repository is allowed')
if args.publisher:
    secret = json.loads(subprocess.check_output(['kubectl', '-n', 'platform-system', 'get', 'secret', 'platform-harbor-provisioning', '-o', 'json']))
    username = base64.b64decode(secret['data']['PLATFORM_HARBOR_USERNAME']).decode()
    password = base64.b64decode(secret['data']['PLATFORM_HARBOR_PASSWORD']).decode()
else:
    secret = json.loads(subprocess.check_output(['kubectl', '-n', 'harbor', 'get', 'secret', 'harbor-secrets', '-o', 'json']))
    username = 'admin'
    password = base64.b64decode(secret['data']['HARBOR_ADMIN_PASSWORD']).decode()
with tempfile.TemporaryDirectory(prefix='codechat-base-push-') as config:
    os.chmod(config, 0o700)
    environment = {**os.environ, 'DOCKER_CONFIG': config}
    login = subprocess.run(['docker', 'login', 'harbor.k3s.local', '-u', username, '--password-stdin'],
        input=password, text=True, capture_output=True, env=environment)
    if login.returncode:
        raise RuntimeError('Registry login failed')
    subprocess.run(['docker', 'tag', args.source, args.target], check=True)
    pushed = subprocess.run(['docker', 'push', args.target], capture_output=True, text=True, env=environment)
    if pushed.returncode:
        raise RuntimeError('Base image publication failed')
    result = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.target]))[0]
    print(json.dumps({'image': args.target, 'digests': result['RepoDigests']}))
