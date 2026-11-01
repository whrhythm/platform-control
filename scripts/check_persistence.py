import json
from pathlib import Path
from uuid import UUID
from cluster_transport import remote

root = Path(__file__).resolve().parents[1]
result = json.loads((root / "release/live-qa-results.json").read_text())
assert result.get("success")
project = str(UUID(result["project"]))
code = """
import json
from sqlalchemy import text
from control_plane.database import engine
with engine.connect() as connection:
    row=connection.execute(text('SELECT id,tenant_id,name FROM projects WHERE id=:id'),{'id':PROJECT}).mappings().one()
    print(json.dumps(dict(row)))
""".replace("PROJECT", repr(project))
query = ["kubectl", "-n", "platform-system", "exec", "deployment/platform-api", "--", "python", "-c", code]
before = json.loads(remote(query))
pods = json.loads(remote(["kubectl", "-n", "platform-system", "get", "pods", "-l", "app.kubernetes.io/name=platform-api", "-o", "json"]))["items"]
assert len(pods) == 1
pod = pods[0]
remote(["kubectl", "-n", "platform-system", "delete", "pod", pod["metadata"]["name"], "--wait=false"])
remote(["kubectl", "-n", "platform-system", "wait", "--for=delete", "pod/" + pod["metadata"]["name"], "--timeout=90s"])
remote(["kubectl", "-n", "platform-system", "rollout", "status", "deployment/platform-api", "--timeout=120s"])
after = json.loads(remote(query))
assert before == after
result["checks"].append("project_persists_across_api_pod_replacement")
(root / "release/live-qa-results.json").write_text(json.dumps(result, indent=2))
print("API Pod replaced; project metadata persisted in PostgreSQL")
