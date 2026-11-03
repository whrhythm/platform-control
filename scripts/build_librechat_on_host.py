"""Transfer the local LibreChat source to edge, verify it, then build and publish."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from cluster_transport import HOST, SOCKET, remote

root = Path(__file__).resolve().parents[1]
source = root.parent / 'LibreChat'
tag = os.environ.get('CODECHAT_IMAGE_TAG', '0.8.9-codechat-20261031.1')
if not tag or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for c in tag):
    raise ValueError('Invalid image tag')
archive = root / 'release/codechat-librechat-build.tar.gz'
subprocess.run(['tar', '-czf', str(archive), '--exclude=.git', '--exclude=node_modules', '--exclude=.claude', '--exclude=.codex', '--exclude=.env', '--exclude=.env.*', '-C', str(source), '.'], check=True)
digest = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(archive.read_bytes()).hexdigest()
destination = '/home/edge/platform-releases/' + tag
remote(['mkdir', '-p', destination + '/context'])
with archive.open('rb') as stream:
    subprocess.run(['ssh', '-S', SOCKET, HOST, f'cat > {destination}/source.tar.gz'], stdin=stream, check=True)
if remote(['sha256sum', destination + '/source.tar.gz']).split()[0] != digest:
    raise RuntimeError('Build context checksum mismatch')
remote(['tar', '-xzf', destination + '/source.tar.gz', '-C', destination + '/context'])
publisher = (root / 'scripts/publish_librechat_custom.py').read_text()
remote(['tee', destination + '/publisher.py'], publisher)
command = (
    'cd ' + shlex.quote(destination + '/context') +
    ' && docker build --network=host --provenance=false --build-arg CODECHAT_VERIFY=true'
    ' --build-arg ALPINE_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/alpine'
    ' --build-arg NPM_REGISTRY=https://registry.npmmirror.com'
    ' --build-arg VITE_CODECHAT_API_ORIGIN=https://platform.k3s.local:30443'
    ' --build-arg VITE_CODECHAT_DEFAULT_LANGUAGE=zh-Hans'
    ' -t codechat-librechat:' + tag + ' .'
    ' && CODECHAT_IMAGE_TAG=' + tag + ' python3 ' + shlex.quote(destination + '/publisher.py') +
    ' > ' + shlex.quote(destination + '/published.json')
)
launch = 'nohup /bin/sh -ec ' + shlex.quote(command) + ' > ' + shlex.quote(destination + '/build.log') + ' 2>&1 < /dev/null & echo $!'
pid = remote(['/bin/sh', '-c', launch]).strip()
print(json.dumps({'context_bytes': archive.stat().st_size, 'sha256': digest, 'build_pid': pid, 'log': destination + '/build.log'}))
