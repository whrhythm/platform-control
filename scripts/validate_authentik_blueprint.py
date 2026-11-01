"""Validate the proposed Authentik blueprint without importing it."""
from pathlib import Path
import yaml
from cluster_transport import remote

path = Path(__file__).resolve().parents[2] / "ops/apps/platform-control/prereqs/authentik-blueprint.yaml"
content = yaml.safe_load(path.read_text(encoding="utf-8"))["data"]["platform.yaml"]
remote(["kubectl", "-n", "authentik", "exec", "-i", "deployment/authentik-worker", "--", "python", "-c", "import sys; open('/tmp/platform-blueprint-check.yaml','w').write(sys.stdin.read())"], content)
command = "from authentik.blueprints.v1.importer import Importer; i=Importer.from_string(open('/tmp/platform-blueprint-check.yaml').read()); valid,logs=i.validate(); print('RESULT:Blueprint valid='+str(valid)); [print('RESULT:'+log.event+': '+str(log.attributes)) for log in logs if not valid]; assert valid"
print(remote(["kubectl", "-n", "authentik", "exec", "deployment/authentik-worker", "--", "env", "AUTHENTIK_LOG_LEVEL=error", "ak", "shell", "-c", command]))
