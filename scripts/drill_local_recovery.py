"""Offline recovery drill on disposable localhost PostgreSQL and synthetic files only.

Never accepts a target URL, kubeconfig or source directory. No production data is read.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
import zipfile
import zlib
from types import SimpleNamespace

import psycopg
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_plane.models import Tenant, UserEnvironment, CapacityAccount, ModelCredential, ProjectVersion
from control_plane.workspace_checkpoints import capture, encode, restore
from control_plane.workspace_files import WorkspaceFiles
from control_plane.project_versions import unpack


def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, **kwargs)


def main():
    suffix = uuid4().hex[:12]
    name = 'platform-recovery-drill-' + suffix
    source_db, restored_db = 'platform_test_' + suffix, 'platform_restore_' + suffix
    password = secrets.token_hex(24)
    created = False
    engines = []
    started = time.monotonic()
    try:
        run('docker', 'run', '--rm', '-d', '--name', name, '--memory=512m', '--cpus=1',
            '-e', 'POSTGRES_PASSWORD=' + password, '-e', 'POSTGRES_DB=' + source_db,
            '-p', '127.0.0.1::5432', os.environ.get('PLATFORM_TEST_POSTGRES_IMAGE', 'postgres:16'),
            stdout=subprocess.DEVNULL)
        created = True
        info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
        port = info['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
        dsn = f'postgresql://postgres:{password}@127.0.0.1:{port}/{source_db}'
        for _ in range(60):
            try:
                with psycopg.connect(dsn, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                time.sleep(.5)
        else:
            raise RuntimeError('Disposable database did not become ready')
        url = dsn.replace('postgresql://', 'postgresql+psycopg://')
        run(sys.executable, '-m', 'alembic', 'upgrade', 'head', cwd=ROOT,
            env={**os.environ, 'PLATFORM_DATABASE_URL': url}, stdout=subprocess.DEVNULL)
        engine = create_engine(url)
        engines.append(engine)
        with tempfile.TemporaryDirectory(prefix='starbridge-recovery-') as temp:
            folder = Path(temp)
            original, restored = folder / 'original', folder / 'restored'
            original.mkdir(); restored.mkdir()
            fixture = {'README.md': '# 恢复演练\n'.encode(), 'src/main.py': b'print("restore-ok")\n',
                       '.settings.json': b'{"synthetic":true}', 'assets/sample.bin': bytes(range(256))}
            digests = {name: hashlib.sha256(content).hexdigest() for name, content in fixture.items()}
            for filename, content in fixture.items():
                path = original / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            recovery_key = Fernet.generate_key()
            key_backup = folder / 'separate-key.backup'
            key_backup.write_bytes(recovery_key)
            key_backup.chmod(0o600)
            with Session(engine) as db, db.begin():
                tenant = Tenant(name='Recovery fixture', owner_issuer='drill', owner_subject=suffix, project_limit=1)
                db.add(tenant); db.flush()
                env = UserEnvironment(issuer='drill', subject=suffix, identity_hash=hashlib.sha256(suffix.encode()).hexdigest(),
                    storage_gib=4, first_tenant_id=tenant.id, first_project_id=None)
                db.add(env); db.flush()
                env_id = env.id
                db.add(CapacityAccount(environment_id=env_id, plan={'id':'basic', 'storage_gib':4, 'application_limit':5}))
                db.add(ModelCredential(issuer='drill', subject=suffix, status='ready',
                    encrypted_key=Fernet(recovery_key).encrypt(b'synthetic-model-key').decode()))
                source_point = capture(WorkspaceFiles(str(original)), 'src')
                packed = encode(source_point)
                encrypted_point = Fernet(recovery_key).encrypt(zlib.compress(packed))
                point_id = str(uuid4())
                db.add(ProjectVersion(id=point_id, environment_id=env_id, request_key='local-drill-'+suffix,
                    project_path='src', name='synthetic point', digest=hashlib.sha256(packed).hexdigest(),
                    manifest={'entries':{},'excluded':[]}, encrypted_content=encrypted_point,
                    stored_bytes=len(encrypted_point),source_bytes=source_point['bytes']))
            backup = folder / 'platform.dump'
            with backup.open('wb') as output:
                run('docker', 'exec', name, 'pg_dump', '-U', 'postgres', '-Fc', source_db, stdout=output)
            archive = folder / 'workspace.zip'
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
                for filename in fixture:
                    output.write(original / filename, filename)
            # Simulate subsequent bad changes; recovery must retain the backup point.
            (original / 'src/main.py').write_text('broken after backup', encoding='utf8')
            with Session(engine) as db, db.begin():
                db.get(CapacityAccount, env_id).plan = {'id':'changed-after-backup', 'storage_gib':1}
            run('docker', 'exec', name, 'createdb', '-U', 'postgres', restored_db)
            with backup.open('rb') as source:
                run('docker', 'exec', '-i', name, 'pg_restore', '-U', 'postgres', '--exit-on-error',
                    '--no-owner', '-d', restored_db, stdin=source)
            with zipfile.ZipFile(archive) as source:
                assert set(source.namelist()) == set(fixture)
                for filename in fixture:
                    target = (restored / filename).resolve()
                    if not target.is_relative_to(restored.resolve()):
                        raise RuntimeError('Unsafe restore path')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read(filename))
            restored_engine = create_engine(url.rsplit('/', 1)[0] + '/' + restored_db)
            engines.append(restored_engine)
            with Session(restored_engine) as db:
                assert db.get(UserEnvironment, env_id).storage_gib == 4
                assert db.get(CapacityAccount, env_id).plan == {'id':'basic', 'storage_gib':4, 'application_limit':5}
                encrypted = db.query(ModelCredential).filter_by(issuer='drill', subject=suffix).one().encrypted_key
                assert Fernet(key_backup.read_bytes()).decrypt(encrypted.encode()) == b'synthetic-model-key'
                point = unpack(db.get(ProjectVersion,point_id),SimpleNamespace(model_credential_encryption_key=key_backup.read_bytes().decode()))
                restore(WorkspaceFiles(str(restored)),'source-history',point,str(uuid4()),'local-drill-token-' + suffix * 3)
                assert (restored/'source-history'/'main.py').read_bytes() == fixture['src/main.py']
                restored_revision = db.execute(text('select version_num from alembic_version')).scalar_one()
            with engine.connect() as db:
                assert restored_revision == db.execute(text('select version_num from alembic_version')).scalar_one()
            assert all(hashlib.sha256((restored / filename).read_bytes()).hexdigest() == digest for filename, digest in digests.items())
            assert (original / 'src/main.py').read_text() == 'broken after backup'
            result = {'status':'passed', 'scope':'synthetic_local_only', 'files_verified':len(fixture),
                'schema_revision':restored_revision, 'identity_and_entitlements_restored':True,
                'separate_key_decryption_verified':True, 'original_not_overwritten':True,
                'version_history_restored':True,
                'database_backup_bytes':backup.stat().st_size, 'elapsed_seconds':round(time.monotonic()-started,2),
                'limitations':['Not a live PV restore', 'Not an off-host backup', 'No production RPO/RTO established']}
            print(json.dumps(result, ensure_ascii=False))
    finally:
        for engine in engines:
            engine.dispose()
        if created:
            run('docker', 'rm', '-f', name, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
