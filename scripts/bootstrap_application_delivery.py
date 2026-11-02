"""One-time creation of a non-admin Harbor publisher account on the cluster host.

The existing administrator credential is used transiently for account creation,
never copied into the publisher secret. Normal project-creation policy is checked
and never changed. Candidate service credentials survive an interrupted bootstrap.
"""
import base64
import json
import os
import secrets
import ssl
import subprocess
import urllib.error
import urllib.request

command = [os.environ.get('KUBECTL_BIN', 'kubectl')]
if os.environ.get('KUBECONFIG'):
    command.extend(['--kubeconfig', os.environ['KUBECONFIG']])
source = json.loads(subprocess.check_output(command + ['-n', 'harbor', 'get', 'secret', 'harbor-secrets', '-o', 'json']))
admin_password = base64.b64decode(source['data']['HARBOR_ADMIN_PASSWORD']).decode()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(
    context=ssl.create_default_context(cafile='/etc/docker/certs.d/harbor.k3s.local/ca.crt')))


def request(username, password, method, path, body=None):
    headers = {'Authorization': 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()}
    if path.startswith('projects/'):
        headers['X-Is-Resource-Name'] = 'true'
    if body is not None:
        headers['Content-Type'] = 'application/json'
    try:
        response = opener.open(urllib.request.Request('https://harbor.k3s.local/api/v2.0/' + path,
            method=method, headers=headers, data=json.dumps(body).encode() if body is not None else None), timeout=15)
        raw = response.read()
        return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read())
        except (ValueError, OSError):
            detail = None
        return error.code, detail


code, policy = request('admin', admin_password, 'GET', 'configurations')
if code != 200 or policy.get('project_creation_restriction', {}).get('value') != 'everyone':
    raise RuntimeError('Ordinary project creation is unavailable; no Harbor policy was changed')
username = 'codechat-provisioner'
existing = subprocess.check_output(command + ['-n', 'platform-system', 'get', 'secret', 'platform-harbor-provisioning', '--ignore-not-found', '-o', 'json'])
if existing.strip():
    data = json.loads(existing)['data']
    if base64.b64decode(data.get('PLATFORM_HARBOR_USERNAME', '')).decode() != username:
        raise RuntimeError('Unexpected publisher identity; refusing to reuse administrator credentials')
    service_password = base64.b64decode(data['PLATFORM_HARBOR_PASSWORD']).decode()
else:
    service_password = 'Cc1!' + secrets.token_urlsafe(48)
    document = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
        'metadata': {'name': 'platform-harbor-provisioning', 'namespace': 'platform-system'},
        'stringData': {'PLATFORM_HARBOR_USERNAME': username, 'PLATFORM_HARBOR_PASSWORD': service_password}}
    subprocess.run(command + ['create', '-f', '-'], input=json.dumps(document).encode(), check=True, stdout=subprocess.DEVNULL)
code, current = request(username, service_password, 'GET', 'users/current')
if code == 401:
    code, detail = request('admin', admin_password, 'POST', 'users', {
        'username': username, 'password': service_password, 'email': 'codechat-provisioner@codechat.invalid',
        'realname': 'CodeChat publisher', 'comment': 'CodeChat'})
    if code != 201:
        reason = json.dumps(detail).replace(admin_password, '[redacted]').replace(service_password, '[redacted]')[:400]
        raise RuntimeError(f'Publisher account creation requires inspection (HTTP {code}): {reason}; no passwords were reset')
    code, current = request(username, service_password, 'GET', 'users/current')
if code != 200 or current.get('username') != username or current.get('sysadmin_flag') is not False:
    raise RuntimeError('Publisher identity is not a verified ordinary Harbor account')
code, catalogs = request(username, service_password, 'GET', 'projects?name=codechat-base')
if code != 200 or not isinstance(catalogs, list):
    raise RuntimeError('Base catalog discovery failed')
catalogs = [item for item in catalogs if item.get('name') == 'codechat-base']
if not catalogs:
    code, _ = request(username, service_password, 'POST', 'projects', {
        'project_name': 'codechat-base', 'metadata': {'public': 'true'}})
    if code != 201:
        raise RuntimeError('Shared upstream-only base catalog creation requires inspection')
    code, catalogs = request(username, service_password, 'GET', 'projects?name=codechat-base')
    if code != 200 or not isinstance(catalogs, list):
        raise RuntimeError('Created catalog verification failed')
catalog = next((item for item in catalogs if item.get('name') == 'codechat-base'), None)
if code != 200 or catalog.get('owner_id') != current['user_id'] or catalog.get('metadata', {}).get('public') != 'true':
    safe = {'http': code, 'creator': catalog.get('owner_id') if catalog else None,
            'current_id': current.get('user_id'), 'public': catalog.get('metadata', {}).get('public') if catalog else None}
    raise RuntimeError('Refusing to reuse an unexpected base image catalog: ' + json.dumps(safe))
print('Non-admin publisher account verified; project creation policy unchanged; administrator credential not copied')
