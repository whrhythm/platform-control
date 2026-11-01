"""Run concurrency tests in a disposable local container, never a business DB."""
import json
import os
import secrets
import subprocess
import sys
import time
from uuid import uuid4

import psycopg


def main():
    suffix = uuid4().hex[:12]
    name = "platform-control-test-" + suffix
    database = "platform_test_" + suffix
    password = secrets.token_hex(24)
    image = os.environ.get("PLATFORM_TEST_POSTGRES_IMAGE", "postgres:17-alpine")
    created = False
    try:
        subprocess.run(["docker", "run", "--rm", "-d", "--name", name, "--memory=512m", "--cpus=1", "-e", "POSTGRES_PASSWORD=" + password, "-e", "POSTGRES_DB=" + database, "-p", "127.0.0.1::5432", image], check=True, stdout=subprocess.DEVNULL)
        created = True
        container = json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]
        port = container["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        dsn = f"postgresql://postgres:{password}@127.0.0.1:{port}/{database}"
        for _ in range(60):
            try:
                with psycopg.connect(dsn, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            raise RuntimeError("Disposable PostgreSQL did not become ready")
        environment = {**os.environ, "PLATFORM_TEST_DATABASE_URL": dsn.replace("postgresql://", "postgresql+psycopg://", 1)}
        return subprocess.run([sys.executable, "-m", "pytest", "tests/test_postgres.py", "-q"], env=environment).returncode
    finally:
        if created:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, check=True)


if __name__ == "__main__":
    sys.exit(main())
