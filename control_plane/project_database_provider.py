"""Recoverable PostgreSQL DDL with immutable provider-side ownership seals."""
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row


class DatabaseOwnershipError(RuntimeError):
    pass


class PostgresProjectProvider:
    def __init__(self, dsn):
        self.dsn = dsn

    @contextmanager
    def locked(self, generation):
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row,
                             connect_timeout=5, options='-c statement_timeout=15000 -c lock_timeout=5000') as conn:
            acquired = conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked',
                                    ('codechat-database:' + generation,)).fetchone()['locked']
            if not acquired:
                raise RuntimeError('Database operation already running')
            try:
                yield conn
            finally:
                conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s, 0))', ('codechat-database:' + generation,))

    def journal(self, conn, generation, owner_hash):
        row = conn.execute('SELECT * FROM codechat_provider.databases WHERE generation=%s', (generation,)).fetchone()
        if row and row['owner_hash'] != owner_hash:
            raise DatabaseOwnershipError()
        return row

    def verify(self, conn, record, database, owner):
        for name, oid in ((owner, record['owner_oid']), (database, record['login_oid'])):
            actual = conn.execute('SELECT oid, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=%s', (name,)).fetchone()
            if not actual or actual['oid'] != oid or actual['rolsuper'] or actual['rolcreatedb'] or actual['rolcreaterole']:
                raise DatabaseOwnershipError()
        actual = conn.execute('SELECT oid, datdba FROM pg_database WHERE datname=%s', (database,)).fetchone()
        if actual and (actual['datdba'] != record['owner_oid'] or
                       record['database_oid'] is not None and actual['oid'] != record['database_oid']):
            raise DatabaseOwnershipError()
        return actual

    def ensure(self, generation, owner_hash, database, owner, password):
        with self.locked(generation) as conn:
            broker = conn.execute('SELECT current_user AS name').fetchone()['name']
            record = self.journal(conn, generation, owner_hash)
            if record is None:
                with conn.transaction():
                    if conn.execute('SELECT 1 FROM pg_roles WHERE rolname IN (%s,%s)', (database, owner)).fetchone() or conn.execute(
                            'SELECT 1 FROM pg_database WHERE datname=%s', (database,)).fetchone():
                        raise DatabaseOwnershipError()
                    verifier = conn.pgconn.encrypt_password(password.encode(), database.encode(), b'scram-sha-256').decode()
                    conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS').format(sql.Identifier(owner)))
                    conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10').format(sql.Identifier(database), sql.Literal(verifier)))
                    for role in (owner, database):
                        conn.execute(sql.SQL('GRANT {} TO {} WITH INHERIT FALSE, SET TRUE').format(sql.Identifier(role), sql.Identifier(broker)))
                    oids = {value['rolname']: value['oid'] for value in conn.execute('SELECT oid,rolname FROM pg_roles WHERE rolname IN (%s,%s)', (owner, database))}
                    conn.execute('INSERT INTO codechat_provider.databases (generation,owner_hash,owner_oid,login_oid,phase) VALUES (%s,%s,%s,%s,%s)',
                        (generation, owner_hash, oids[owner], oids[database], 'creating'))
                record = self.journal(conn, generation, owner_hash)
            if record['phase'] == 'deleted':
                raise DatabaseOwnershipError()
            actual = self.verify(conn, record, database, owner)
            if actual is None:
                if record['database_oid'] is not None:
                    raise DatabaseOwnershipError()
                conn.execute(sql.SQL('CREATE DATABASE {} OWNER {} TEMPLATE template0').format(sql.Identifier(database), sql.Identifier(owner)))
                actual = self.verify(conn, record, database, owner)
            conn.execute('UPDATE codechat_provider.databases SET database_oid=%s WHERE generation=%s', (actual['oid'], generation))
            # Repeating grants is safe after a crash between CREATE DATABASE and readiness.
            conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
            conn.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(database)))
            conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}, {}').format(sql.Identifier(database), sql.Identifier(database), sql.Identifier(broker)))
            conn.execute('RESET ROLE')
            options = conninfo_to_dict(self.dsn)
            options.update(dbname=database, connect_timeout=5, options='-c statement_timeout=15000 -c lock_timeout=5000')
            with psycopg.connect(**options) as project:
                project.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
                project.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
                project.execute(sql.SQL('GRANT USAGE, CREATE ON SCHEMA public TO {}').format(sql.Identifier(database)))
            conn.execute("UPDATE codechat_provider.databases SET phase='ready' WHERE generation=%s", (generation,))
            return {'database_oid': actual['oid'], 'owner_oid': record['owner_oid'], 'login_oid': record['login_oid']}

    def size(self, generation, owner_hash, database, owner):
        with self.locked(generation) as conn:
            record = self.journal(conn, generation, owner_hash)
            if record is None or record['phase'] != 'ready' or self.verify(conn, record, database, owner) is None:
                raise DatabaseOwnershipError()
            return conn.execute('SELECT pg_database_size(%s) AS size', (database,)).fetchone()['size']

    def delete(self, generation, owner_hash, database, owner):
        with self.locked(generation) as conn:
            record = self.journal(conn, generation, owner_hash)
            if record is None:
                # Nothing was created by this provider; do not touch name collisions.
                return
            if record['phase'] == 'deleted':
                return
            # Deletion recovery may already have dropped roles. Validate each remaining OID.
            actual = conn.execute('SELECT oid, datdba FROM pg_database WHERE datname=%s', (database,)).fetchone()
            if actual and (actual['oid'] != record['database_oid'] and record['database_oid'] is not None or actual['datdba'] != record['owner_oid']):
                raise DatabaseOwnershipError()
            for name, oid in ((owner, record['owner_oid']), (database, record['login_oid'])):
                role = conn.execute('SELECT oid FROM pg_roles WHERE rolname=%s', (name,)).fetchone()
                if role and role['oid'] != oid:
                    raise DatabaseOwnershipError()
            if actual:
                conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
                conn.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS false').format(sql.Identifier(database)))
                conn.execute('RESET ROLE')
                conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(database)))
                conn.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datid=%s AND usesysid=%s AND pid<>pg_backend_pid()',
                             (actual['oid'], record['login_oid']))
                conn.execute('RESET ROLE')
                conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
                conn.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(database)))
                conn.execute('RESET ROLE')
            with conn.transaction():
                for name in (database, owner):
                    conn.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(name)))
                conn.execute("UPDATE codechat_provider.databases SET phase='deleted' WHERE generation=%s", (generation,))
