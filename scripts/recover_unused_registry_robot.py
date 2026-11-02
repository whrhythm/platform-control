"""Recover one newly provisioned, unused CodeChat robot after a reviewed failure.

Run inside the publisher pod. Refuses any environment with application records;
only the exact project-level robot created for this environment may be refreshed.
"""
import argparse
import ssl

from cryptography.fernet import Fernet
import httpx
from sqlalchemy import func, select

from control_plane.database import SessionLocal
from control_plane.models import AuditEvent, UserApplication, UserEnvironment, UserRegistry
from control_plane.settings import get_settings

parser = argparse.ArgumentParser()
parser.add_argument('--environment-id', required=True)
args = parser.parse_args()
settings = get_settings()
with SessionLocal() as db:
    binding = db.get(UserRegistry, args.environment_id)
    env = db.get(UserEnvironment, args.environment_id)
    count = db.scalar(select(func.count()).select_from(UserApplication).where(UserApplication.environment_id == args.environment_id))
    if not binding or not env or binding.status != 'requires_review' or count != 0:
        raise RuntimeError('Refusing credential recovery for a used or unreviewed environment')
    project, candidate, project_id = binding.project_name, binding.robot_name, binding.project_id
with httpx.Client(base_url=settings.harbor_url, verify=ssl.create_default_context(cafile=settings.workspace_service_ca),
                  auth=(settings.harbor_username, settings.harbor_password), timeout=15, trust_env=False) as client:
    response = client.get('/api/v2.0/robots', params={'q': 'Level=project,ProjectID=' + str(project_id), 'page_size': 100})
    if response.status_code != 200:
        raise RuntimeError('Robot lookup failed HTTP ' + str(response.status_code))
    matches = [item for item in response.json() if item.get('name', '').endswith(candidate)
        and item.get('description') == 'CodeChat environment ' + env.id and item.get('level') == 'project'
        and len(item.get('permissions', [])) == 1 and item['permissions'][0].get('namespace') == project]
    if len(matches) != 1:
        raise RuntimeError('Refusing recovery without exact robot ownership')
    robot = matches[0]
    with SessionLocal.begin() as db:
        row = db.get(UserRegistry, env.id)
        row.status = 'robot_recovery_inflight'
    client.cookies.clear()
    response = client.patch('/api/v2.0/robots/' + str(robot['id']), json={})
    data = response.json() if response.status_code == 200 else {}
    if not isinstance(data.get('secret'), str) or not 16 <= len(data['secret']) <= 512:
        raise RuntimeError('Credential refresh outcome requires review')
    with SessionLocal.begin() as db:
        row = db.get(UserRegistry, env.id)
        row.robot_id, row.robot_name = robot['id'], robot['name']
        row.encrypted_secret = Fernet(settings.model_credential_encryption_key.encode()).encrypt(data['secret'].encode()).decode()
        row.status = 'ready'
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=env.issuer, subject=env.subject,
            action='registry.credential.recovered', target_id=env.id))
print('Recovered only the exact unused project robot; credential encrypted; no secret values printed')
