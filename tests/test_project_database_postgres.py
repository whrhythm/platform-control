"""Provider tests run ONLY inside scripts/test_postgres.py's disposable local DB."""
import os
import secrets
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from control_plane.project_databases import names
from control_plane.project_database_provider import PostgresProjectProvider, DatabaseOwnershipError


@pytest.fixture(scope='module')
def provider():
    raw = os.environ.get('PLATFORM_TEST_DATABASE_URL', '').replace('postgresql+psycopg://', 'postgresql://')
    if not raw:
        pytest.skip('Disposable local PostgreSQL only')
    options = conninfo_to_dict(raw)
    assert options['host'] in ('127.0.0.1', 'localhost') and options['dbname'].startswith('platform_test_')
    options['dbname'] = 'postgres'
    password = secrets.token_hex(24)
    with psycopg.connect(**options, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE ROLE test_db_provider LOGIN PASSWORD {} CREATEDB CREATEROLE NOSUPERUSER').format(sql.Literal(password)))
        conn.execute('CREATE SCHEMA codechat_provider AUTHORIZATION test_db_provider')
        conn.execute('SET ROLE test_db_provider')
        conn.execute('CREATE TABLE codechat_provider.databases (generation uuid PRIMARY KEY, owner_hash varchar(64), owner_oid oid, login_oid oid, database_oid oid, phase varchar(16))')
    options.update(user='test_db_provider', password=password)
    return PostgresProjectProvider(make_conninfo(**options))


def test_owned_database_login_isolation_size_recovery_and_destroy(provider):
    generation, other = str(uuid4()), str(uuid4())
    database, owner, _ = names(SimpleNamespace(generation=generation))
    other_db, other_owner, _ = names(SimpleNamespace(generation=other))
    password = secrets.token_hex(24)
    evidence = provider.ensure(generation, 'a' * 64, database, owner, password)
    assert provider.ensure(generation, 'a' * 64, database, owner, password) == evidence
    provider.ensure(other, 'b' * 64, other_db, other_owner, secrets.token_hex(24))
    options = conninfo_to_dict(provider.dsn)
    options.update(user=database, password=password, dbname=database)
    with psycopg.connect(**options, autocommit=True) as conn:
        conn.execute('CREATE TABLE counter (value integer)')
        conn.execute('INSERT INTO counter VALUES (42)')
        assert conn.execute('SELECT value FROM counter').fetchone()[0] == 42
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('CREATE ROLE should_never_exist')
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**{**options, 'dbname': other_db}).close()
    assert provider.size(generation, 'a' * 64, database, owner) > 0
    with pytest.raises(DatabaseOwnershipError):
        provider.delete(generation, 'b' * 64, database, owner)
    lingering = psycopg.connect(**options)
    provider.delete(generation, 'a' * 64, database, owner)
    with pytest.raises(psycopg.OperationalError):
        lingering.execute('SELECT 1')
    lingering.close()
    provider.delete(generation, 'a' * 64, database, owner)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**options).close()
    provider.delete(other, 'b' * 64, other_db, other_owner)


def test_unowned_role_collision_never_adopted(provider):
    generation = str(uuid4())
    database, owner, _ = names(SimpleNamespace(generation=generation))
    with psycopg.connect(provider.dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(owner)))
    with pytest.raises(DatabaseOwnershipError):
        provider.ensure(generation, 'a' * 64, database, owner, 'unused')
    with psycopg.connect(provider.dsn, autocommit=True) as conn:
        assert conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (owner,)).fetchone()
        conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(owner)))
