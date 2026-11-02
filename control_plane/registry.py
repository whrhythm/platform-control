"""Journalled private Harbor project/robot provisioning for one environment."""
from urllib.parse import quote

from cryptography.fernet import Fernet

from .models import UserEnvironment, UserRegistry


class RegistryError(RuntimeError):
    pass


def provision_registry(sessions, env_id, client, encryption_key):
    cipher = Fernet(encryption_key.encode())
    with sessions.begin() as db:
        env = db.get(UserEnvironment, env_id)
        binding = db.get(UserRegistry, env_id)
        if binding is None:
            binding = UserRegistry(environment_id=env_id, project_name='u-' + env.identity_hash[:24],
                robot_name='codechat-' + env.identity_hash[:24],
                encrypted_secret=cipher.encrypt(b'').decode())
            db.add(binding)
        db.flush()
        status = binding.status
        project, robot = binding.project_name, binding.robot_name
        if status == 'ready':
            return
        if status.endswith('_inflight'):
            raise RegistryError('registry_write_requires_review')
        if status == 'requested':
            binding.status = 'project_inflight'
    if status == 'requested':
        client.cookies.clear()  # Server-to-server Basic auth does not use browser sessions.
        response = client.post('/api/v2.0/projects', json={'project_name': project, 'metadata': {'public': 'false'},
                                                        'storage_limit': 2 * 1024 ** 3})
        if response.status_code != 201:
            raise RegistryError('registry_project_create_http_' + str(response.status_code))
        response = client.get('/api/v2.0/projects/' + quote(project), headers={'X-Is-Resource-Name': 'true'})
        data = response.json() if response.status_code == 200 else {}
        if data.get('name') != project or not isinstance(data.get('project_id'), int) or data.get('metadata', {}).get('public') != 'false':
            raise RegistryError('registry_project_verify_failed')
        with sessions.begin() as db:
            binding = db.get(UserRegistry, env_id)
            binding.project_id, binding.status = data['project_id'], 'project_created'
    with sessions.begin() as db:
        binding = db.get(UserRegistry, env_id)
        if binding.status != 'project_created':
            raise RegistryError('registry_state_requires_review')
        binding.status = 'robot_inflight'
    client.cookies.clear()
    response = client.post('/api/v2.0/robots', json={
        'name': robot, 'description': 'CodeChat environment ' + env_id,
        'level': 'project', 'duration': -1, 'disable': False,
        'permissions': [{'kind': 'project', 'namespace': project,
                         'access': [{'resource': 'repository', 'action': action} for action in ('pull', 'push')]}],
    })
    data = response.json() if response.status_code == 201 else {}
    if not isinstance(data.get('id'), int) or not isinstance(data.get('name'), str) or not data['name'].endswith(robot):
        raise RegistryError('registry_robot_create_http_' + str(response.status_code))
    if not isinstance(data.get('secret'), str) or not 16 <= len(data['secret']) <= 512:
        raise RegistryError('registry_robot_secret_requires_review')
    with sessions.begin() as db:
        binding = db.get(UserRegistry, env_id)
        binding.robot_id, binding.robot_name, binding.status = data['id'], data['name'], 'ready'
        binding.encrypted_secret = cipher.encrypt(data['secret'].encode()).decode()
