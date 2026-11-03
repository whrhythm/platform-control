"""Increase an existing managed worker budget without rotating credentials."""
from cluster_transport import remote


def main():
    query = """
DO $budget$
DECLARE current_limit integer;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba
                 WHERE d.datname='platform_control' AND r.rolname='platform_migrator') THEN
    RAISE EXCEPTION 'Managed database ownership differs';
  END IF;
  SELECT rolconnlimit INTO current_limit FROM pg_roles WHERE rolname='platform_worker'
      AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication;
  IF current_limit IS NULL THEN RAISE EXCEPTION 'Managed worker identity differs'; END IF;
  IF current_limit >= 0 AND current_limit < 16 THEN
    ALTER ROLE platform_worker CONNECTION LIMIT 16;
  END IF;
END $budget$;
SELECT 'Worker connection budget verified: ' || rolconnlimit FROM pg_roles WHERE rolname='platform_worker';
"""
    print(remote(['kubectl', '-n', 'postgres', 'exec', '-i', 'postgres-1', '-c', 'postgres', '--',
                  'psql', '-X', '-q', '-At', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'postgres'], query).strip())


if __name__ == '__main__':
    main()
