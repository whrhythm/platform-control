"""Integration tests use a disposable local server, never the cluster's data."""
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from uuid import uuid4


def main():
    name = 'codechat-minio-test-' + uuid4().hex[:12]
    password = secrets.token_urlsafe(36)
    image = os.environ.get('CODECHAT_MINIO_TEST_IMAGE', 'docker.1ms.run/bitnamilegacy/minio:2025.5.24-debian-12-r0')
    created = False
    try:
        subprocess.run(['docker', 'run', '--rm', '-d', '--name', name, '--memory=768m', '--cpus=1',
            '-p', '127.0.0.1::9000', '-e', 'MINIO_ROOT_USER=codechat-test-root', '-e', 'MINIO_ROOT_PASSWORD=' + password,
            '-e', 'MINIO_BROWSER=off', image], check=True, stdout=subprocess.DEVNULL)
        created = True
        actual = json.loads(subprocess.check_output(['docker', 'inspect', name], text=True))[0]
        port = actual['NetworkSettings']['Ports']['9000/tcp'][0]['HostPort']
        endpoint = '127.0.0.1:' + port
        for _ in range(60):
            try:
                with urllib.request.urlopen('http://' + endpoint + '/minio/health/ready', timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError('Disposable object server did not become ready')
        environment = {**os.environ, 'CODECHAT_MINIO_TEST_ENDPOINT': endpoint,
                       'CODECHAT_MINIO_TEST_ROOT': 'codechat-test-root', 'CODECHAT_MINIO_TEST_PASSWORD': password}
        return subprocess.run([sys.executable, '-m', 'pytest', 'tests/test_object_minio.py', '-q', '--tb=short'], env=environment).returncode
    finally:
        if created:
            subprocess.run(['docker', 'rm', '-f', name], check=True, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    sys.exit(main())
