"""Remove owned QA metadata through the operator DB socket; keep audit."""
import json
from pathlib import Path
from uuid import UUID
from psycopg import sql
from cluster_transport import remote

root = Path(__file__).resolve().parents[1]
path = root / "release/live-qa-results.json"
result = json.loads(path.read_text())
statements = ["BEGIN;"]
for tenant in result["tenants"]:
    identifier = sql.Literal(str(UUID(tenant["id"]))).as_string()
    issuer = sql.Literal(tenant["issuer"]).as_string()
    subject = sql.Literal(tenant["subject"]).as_string()
    statements.append(f"DO $check$ BEGIN IF NOT EXISTS (SELECT 1 FROM tenants WHERE id={identifier} AND owner_issuer={issuer} AND owner_subject={subject}) THEN RAISE EXCEPTION 'QA tenant owner mismatch; refusing deletion'; END IF; END $check$;")
    for table in ["operations", "resources", "projects", "memberships"]:
        statements.append(f"DELETE FROM {sql.Identifier(table).as_string()} WHERE tenant_id={identifier};")
    statements.append(f"DELETE FROM tenants WHERE id={identifier};")
statements.append("COMMIT;")
remote(["kubectl", "-n", "postgres", "exec", "-i", "postgres-1", "-c", "postgres", "--", "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "platform_control"], "\n".join(statements))
result["qa_metadata_removed"] = True
path.write_text(json.dumps(result, indent=2))
print("Marked QA metadata removed; audit retained; no elevated credentials entered runtime Pods")
