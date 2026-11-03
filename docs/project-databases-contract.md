# Project databases: first delivery

Each existing project directory may request one database on shared PostgreSQL.
The authenticated platform identity determines the personal environment; callers
cannot select an environment, database name, role, host, password or SQL statement.

`GET /internal/v1/workspace/databases` returns public lifecycle and measurement
metadata. `POST` with `{project_path}` returns 202, or the same existing intent on
repeat calls. A failed intent may be retried using `POST /{id}/retry`.
`POST /{id}/delete` requires `{confirmation: project_path}` and no active application
for that directory. Destruction is explicit and does not follow application failure,
stop or deletion. Responses never contain a connection password.

The same actions are available under `/agent/v1/databases` to the owner's issued
model credential. Only this authenticated agent surface exposes `GET /profile?path=`,
used by the runtime helper to start a child process with database environment
variables. No credentials are written into source, chat history or build contexts.
The next publication of the same project automatically receives its connection
Secret and a verifying PostgreSQL CA mount. Existing applications are not restarted
merely because a database has been requested.

Initial allowance: two reservations of 128 MiB per personal environment. This is
an additional 256 MiB database allowance, separate from the existing file-volume
allowance. Actual usage is `pg_database_size`; stale measurements remain stale,
never zero. PostgreSQL does not provide a hard per-database byte quota here; the
reservation limits allocation and the UI reports over-budget use. A hard write
quota and paid database allowance upgrades are not delivered in this iteration.
Object storage, Harbor images and application volumes remain explicitly unmetered.

The controller uses a non-superuser provisioning role. Roles and a durable provider
journal are created transactionally; database creation is recovered only against
the recorded owner-role OID. Destruction checks the recorded database and role OIDs.
Name collisions require administrator review and are never adopted or dropped.

Provider references: connection rules use PostgreSQL's
[`sameuser` and role regular expressions](https://www.postgresql.org/docs/17/auth-pg-hba-conf.html);
password verifiers are generated using psycopg's
[`PGconn.encrypt_password`](https://www.psycopg.org/psycopg3/docs/api/pq.html).
