"""Create/remove two marked, temporary identities for real PKCE acceptance."""
import json
import os
from pathlib import Path
import secrets
import sys
from uuid import uuid4
from cluster_transport import remote

root = Path(__file__).resolve().parents[1]
credentials = root / "release/live-qa-credentials.json"
mode = sys.argv[1] if len(sys.argv) > 1 else "prepare"

if mode == "prepare":
    if credentials.exists():
        payload = json.loads(credentials.read_text())
    else:
        run = uuid4().hex[:12]
        payload = {"run": run, "users": [{"username": f"platform-qa-{run}-{suffix}", "password": secrets.token_urlsafe(24), "admin": suffix == "a"} for suffix in ("a", "b")]}
        credentials.write_text(json.dumps(payload))
        os.chmod(credentials, 0o600)
    code = """
import json
from authentik.core.models import Group,User
payload=json.loads(PAYLOAD)
for identity in payload['users']:
    user,created=User.objects.get_or_create(username=identity['username'],defaults={'name':'Platform integration QA','email':identity['username']+'@example.test','attributes':{'platform_qa_run':payload['run']}})
    assert user.attributes.get('platform_qa_run')==payload['run']
    user.set_password(identity['password'])
    user.save()
    if identity['admin']: Group.objects.get(name='platform-admins').users.add(user)
print('RESULT:Created two temporary marked QA identities')
""".replace("PAYLOAD", repr(json.dumps(payload)))
elif mode == "cleanup":
    payload = json.loads(credentials.read_text())
    code = """
import json
from authentik.core.models import User
payload=json.loads(PAYLOAD)
for identity in payload['users']:
    user=User.objects.filter(username=identity['username']).first()
    if user:
        assert user.attributes.get('platform_qa_run')==payload['run']
        user.delete()
print('RESULT:Temporary QA identities removed')
""".replace("PAYLOAD", repr(json.dumps(payload)))
else:
    raise SystemExit("Use prepare or cleanup")

output = remote(["kubectl", "-n", "authentik", "exec", "-i", "deployment/authentik-worker", "--", "env", "AUTHENTIK_LOG_LEVEL=error", "ak", "shell"], code, redact=tuple(user["password"] for user in payload["users"]))
for line in output.splitlines():
    if line.startswith("RESULT:"):
        print(line)
if mode == "cleanup":
    credentials.unlink()
