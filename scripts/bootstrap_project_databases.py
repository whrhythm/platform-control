"""Operator bootstrap: sealed non-superuser provisioner, never prints credentials."""
import json
import secrets
from urllib.parse import quote, unquote, urlsplit

from psycopg import sql

from bootstrap_cluster import get_secret, psql
from cluster_transport import remote


def main():
    role = 'codechat_db_provisioner'
    secret = get_secret('platform-system', 'platform-project-database-provisioner')
    if secret:
        import base64
        dsn = base64.b64decode(secret['data']['PLATFORM_PROJECT_DATABASE_PROVISIONER_URL']).decode()
        parsed = urlsplit(dsn)
        if parsed.username != role or parsed.hostname != 'postgres-rw.postgres.svc.cluster.local' or parsed.path != '/postgres':
            raise RuntimeError('Unexpected existing provisioner identity')
        password = unquote(parsed.password)
    else:
        password = secrets.token_urlsafe(48)
        dsn = f'postgresql://{role}:{quote(password, safe="")}@postgres-rw.postgres.svc.cluster.local:5432/postgres?sslmode=verify-full&sslrootcert=/etc/platform-ca/postgres-ca.crt&connect_timeout=5'
        prior = psql("SELECT 1 FROM pg_roles WHERE rolname='codechat_db_provisioner';").strip()
        if prior:
            raise RuntimeError('Unrecorded provisioner role already exists; no password reset performed')
        remote(['kubectl', 'create', '-f', '-'], json.dumps({'apiVersion': 'v1', 'kind': 'Secret',
            'metadata': {'name': 'platform-project-database-provisioner', 'namespace': 'platform-system'},
            'type': 'Opaque', 'stringData': {'PLATFORM_PROJECT_DATABASE_PROVISIONER_URL': dsn}}), redact=(password, dsn))
    existing = psql("SELECT rolsuper::text || ',' || rolcreatedb::text || ',' || rolcreaterole::text FROM pg_roles WHERE rolname='codechat_db_provisioner';").strip()
    if not existing:
        query = sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER CREATEDB CREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8;').format(sql.Identifier(role), sql.Literal(password)).as_string()
        psql(query, redact=(password,))
    elif existing != 'false,true,true':
        raise RuntimeError('Unexpected provisioner privileges')
    owner = psql("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='codechat_provider';").strip()
    if owner and owner != role:
        raise RuntimeError('Provider journal schema has another owner')
    psql('''CREATE SCHEMA IF NOT EXISTS codechat_provider AUTHORIZATION codechat_db_provisioner;
REVOKE ALL ON SCHEMA codechat_provider FROM PUBLIC;
SET ROLE codechat_db_provisioner;
CREATE TABLE IF NOT EXISTS codechat_provider.databases (
 generation uuid PRIMARY KEY, owner_hash varchar(64) NOT NULL,
 owner_oid oid NOT NULL, login_oid oid NOT NULL, database_oid oid,
 phase varchar(16) NOT NULL CHECK (phase IN ('creating','ready','deleted')));
REVOKE ALL ON codechat_provider.databases FROM PUBLIC;
RESET ROLE;
GRANT CONNECT ON DATABASE postgres TO codechat_db_provisioner;''')
    print('Provisioner and durable ownership journal ready. Credential values were not printed.')


if __name__ == '__main__':
    main()
