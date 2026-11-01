"""Remove exact disposable self-registration QA identities, never an administrator."""
import json
from pathlib import Path
import re
from cluster_transport import remote

path = Path(__file__).resolve().parents[1] / "release/live-qa-credentials.json"
payload = json.loads(path.read_text())
run = payload["run"]
if not re.fullmatch(r"[0-9a-f]{12}", run):
    raise RuntimeError("Invalid QA run marker")
names = [f"platform-qa-{run}-{suffix}" for suffix in ("a", "b")]
program = """
import json
from authentik.core.models import User
for name in json.loads(NAMES):
    user = User.objects.filter(username=name).first()
    if user:
        assert user.email == name + '@example.test'
        assert not user.is_superuser
        user.delete()
print('RESULT:Removed disposable enrollment identities')
""".replace("NAMES", repr(json.dumps(names)))
output = remote(["kubectl", "-n", "authentik", "exec", "-i", "deployment/authentik-worker", "--", "env", "AUTHENTIK_LOG_LEVEL=error", "ak", "shell"], program)
if "RESULT:Removed disposable enrollment identities" not in output:
    raise RuntimeError("Identity cleanup not confirmed")
path.unlink()
print("Removed disposable enrollment identities and local credentials")
