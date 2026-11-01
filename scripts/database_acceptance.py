# Execute inside each platform container; never prints connection strings.
import json
import os
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from control_plane.database import engine

with engine.connect() as connection:
    role = connection.execute(text("SELECT current_user, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user")).one()
    assert not any(role[1:])
    assert connection.scalar(text("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"))
    assert not connection.scalar(text("SELECT has_schema_privilege(current_user,'public','CREATE')"))
    assert not connection.scalar(text("SELECT has_table_privilege(current_user,'audit_events','UPDATE')"))
    assert not connection.scalar(text("SELECT has_table_privilege(current_user,'audit_events','DELETE')"))
assert engine.url.query.get("sslmode") == "verify-full"
other = create_engine(engine.url.set(database="postgres"))
denied = False
try:
    with other.connect():
        pass
except OperationalError as error:
    denied = "pg_hba.conf rejects connection" in str(error.orig)
finally:
    other.dispose()
assert denied, "Runtime identity must not connect to another database"
assert os.getuid() == 10001
assert not Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists()
print(json.dumps({"role": role[0], "tls": "verify-full", "cross_database_denied": True, "non_root": True, "kubernetes_token_mounted": False, "audit_mutation_denied": True}))
