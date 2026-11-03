"""Idempotent operator bootstrap via SSH; never prints credential values."""
import base64
import json
import secrets
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import yaml
from psycopg import sql

from cluster_transport import remote

NAMESPACE = "platform-system"
DATABASE = "platform_control"
ROLES = {"migrator": ("platform_migrator", 3), "api": ("platform_api", 20), "worker": ("platform_worker", 16)}


def get_secret(namespace, name):
    value = remote(["kubectl", "-n", namespace, "get", "secret", name, "--ignore-not-found", "-o", "json"])
    return json.loads(value) if value.strip() else None


def apply(document):
    remote(["kubectl", "apply", "-f", "-"], json.dumps(document))


def psql(query, database="postgres", redact=()):
    return remote(["kubectl", "-n", "postgres", "exec", "-i", "postgres-1", "-c", "postgres", "--", "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database], query, redact=redact)


def main():
    repo = Path(__file__).resolve().parents[2] / "ops"
    namespace = yaml.safe_load((repo / "apps/platform-control/prereqs/namespace.yaml").read_text())
    apply(namespace)
    prior_roles = set(psql("SELECT rolname FROM pg_roles WHERE rolname IN ('platform_migrator','platform_api','platform_worker');").splitlines())
    owner = psql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'platform_control';").strip()
    if owner and owner != "platform_migrator":
        raise RuntimeError("Existing platform_control database has another owner; refusing to modify it")
    passwords = {}
    existing = {}
    for component, (role, _) in ROLES.items():
        secret = get_secret(NAMESPACE, "platform-db-" + component)
        existing[component] = secret
        if role in prior_roles and secret is None:
            raise RuntimeError(f"Managed role {role} exists without its Secret; refusing automatic password rotation")
        if secret:
            url = base64.b64decode(secret["data"]["PLATFORM_DATABASE_URL"]).decode()
            parsed = urlsplit(url)
            if parsed.username != role or parsed.path != "/" + DATABASE:
                raise RuntimeError("Existing managed database Secret has an unexpected identity")
            passwords[component] = unquote(parsed.password)
        else:
            passwords[component] = secrets.token_hex(24)
    # Persist generated credentials before external role creation so a lost SSH
    # connection cannot leave a role whose password has been forgotten.
    for component, (role, _) in ROLES.items():
        if existing[component]:
            continue
        password = passwords[component]
        url = f"postgresql+psycopg://{role}:{quote(password, safe='')}@postgres-rw.postgres.svc.cluster.local:5432/{DATABASE}?sslmode=verify-full&sslrootcert=/etc/platform-ca/postgres-ca.crt&connect_timeout=5"
        document = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "platform-db-" + component, "namespace": NAMESPACE}, "type": "Opaque", "stringData": {"PLATFORM_DATABASE_URL": url}}
        remote(["kubectl", "create", "-f", "-"], json.dumps(document), redact=(password, url))
    for component, (role, limit) in ROLES.items():
        password = passwords[component]
        literal = sql.Literal(password).as_string()
        identifier = sql.Identifier(role).as_string()
        psql(f"""
DO $block$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
    CREATE ROLE {identifier} LOGIN PASSWORD {literal};
  END IF;
END $block$;
ALTER ROLE {identifier} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT {limit};
ALTER ROLE {identifier} SET statement_timeout = '30s';
ALTER ROLE {identifier} SET idle_in_transaction_session_timeout = '30s';
""", redact=(password,))
    psql("SELECT 'CREATE DATABASE platform_control OWNER platform_migrator' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'platform_control')\\gexec\nREVOKE ALL ON DATABASE platform_control FROM PUBLIC;\nGRANT CONNECT ON DATABASE platform_control TO platform_migrator, platform_api, platform_worker;\n")
    psql("REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n", database=DATABASE)
    root = get_secret("cert-manager", "internal-ca")
    pg_ca = get_secret("postgres", "postgres-ca")
    root_pem = base64.b64decode(root["data"]["tls.crt"]).decode()
    pg_pem = base64.b64decode(pg_ca["data"]["ca.crt"]).decode()
    apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "platform-ca", "namespace": NAMESPACE}, "data": {"internal-ca.crt": root_pem, "postgres-ca.crt": pg_pem}})
    blueprint = yaml.safe_load((repo / "apps/platform-control/prereqs/authentik-blueprint.yaml").read_text())
    apply(blueprint)
    # Validate before mounting it into the shared Authentik deployments.
    remote(["kubectl", "-n", "authentik", "exec", "-i", "deployment/authentik-worker", "--", "python", "-c", "import sys; open('/tmp/platform-blueprint.yaml','w').write(sys.stdin.read())"], blueprint["data"]["platform.yaml"])
    validation = "from authentik.blueprints.v1.importer import Importer; importer=Importer.from_string(open('/tmp/platform-blueprint.yaml').read()); valid,logs=importer.validate(); print('RESULT:Blueprint valid=' + str(valid)); [print('RESULT:' + log.event + ': ' + str(log.attributes)) for log in logs if not valid]; assert valid"
    output = remote(["kubectl", "-n", "authentik", "exec", "deployment/authentik-worker", "--", "env", "AUTHENTIK_LOG_LEVEL=error", "ak", "shell", "-c", validation])
    if '"level": "error"' in output or "failed" in output.lower():
        raise RuntimeError("Authentik blueprint validation failed: " + output[-2500:])
    print("Platform bootstrap complete: dedicated database, three runtime identities, CA trust and validated OIDC blueprint. Credential values were not printed.")


if __name__ == "__main__":
    main()
