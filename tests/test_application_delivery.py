import io
import json
import os
import tarfile
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from control_plane.application_runtime import WRITE_REVIEW_CODE, application_objects, build_objects
from control_plane.application_observation import quantity
from control_plane.applications import release_database
from control_plane.project_databases import names as database_names
from control_plane.database import make_engine
from control_plane.models import Base, Tenant, UserEnvironment, UserRegistry
from control_plane.registry import provision_registry, RegistryError
from control_plane.settings import Settings
from control_plane.workspace_files import WorkspaceFiles, UploadTooLarge
from control_plane.workspace_snapshot import snapshot
from control_plane.user_environments import identity_digest


def test_source_snapshot_is_real_bounded_and_does_not_follow_links(tmp_path):
    project = tmp_path / 'demo'
    project.mkdir()
    (project / 'Dockerfile').write_text('FROM scratch\nCOPY app.txt /app.txt\n')
    (project / 'app.txt').write_text('actual source')
    (project / '.git').mkdir()
    (project / '.git' / 'config').write_text('exclude')
    try:
        (project / 'outside').symlink_to('/etc/passwd')
    except (OSError, NotImplementedError):
        pass
    archive = snapshot(WorkspaceFiles(str(tmp_path)), 'demo')
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        assert set(source.getnames()) == {'Dockerfile', 'app.txt'}
        assert source.extractfile('app.txt').read() == b'actual source'
    assert snapshot(WorkspaceFiles(str(tmp_path)), 'demo') == archive
    with pytest.raises(UploadTooLarge):
        snapshot(WorkspaceFiles(str(tmp_path)), 'demo', max_bytes=1)


def test_build_job_uses_independent_kata_and_no_host_socket():
    app = SimpleNamespace(id='a' * 36, revision='b' * 36, project_path='demo', image='registry/p@sha256:' + 'c' * 64, desired_state='running')
    env = SimpleNamespace(identity_hash='d' * 64)
    registry = SimpleNamespace(robot_name='robot$demo', project_name='u-demo')
    settings = Settings(buildkit_image='registry/buildkit@sha256:' + 'e' * 64, workspace_service_image='registry/platform@sha256:' + 'f' * 64,
                        workspace_pip_index_url='https://packages.test/simple')
    job, secret, target = build_objects(app, env, registry, settings, 'synthetic-secret', 'synthetic-workspace-token', 'test-ca')
    pod = job['spec']['template']['spec']
    assert pod['runtimeClassName'] == 'kata-qemu-runtime-rs'
    assert pod['automountServiceAccountToken'] is False
    assert 'build-arg:PIP_INDEX_URL=https://packages.test/simple' in pod['containers'][0]['args']
    assert 'size=1073741824' in pod['containers'][0]['command'][2]
    settings.build_scratch_mib, settings.build_memory_mib = 2048, 3072
    larger, _, _ = build_objects(app, env, registry, settings, 'synthetic-secret', 'synthetic-workspace-token', 'test-ca')
    assert 'size=2147483648' in larger['spec']['template']['spec']['containers'][0]['command'][2]
    assert larger['spec']['template']['spec']['containers'][0]['resources']['limits']['memory'] == '3072Mi'
    settings.build_memory_mib = 2048
    with pytest.raises(ValueError):
        build_objects(app, env, registry, settings, 'synthetic-secret', 'synthetic-workspace-token', 'test-ca')
    assert all('hostPath' not in volume for volume in pod['volumes'])
    assert target.startswith('harbor.k3s.local/u-demo/')
    assert 'synthetic-secret' not in json.dumps(job)
    objects = application_objects(app, env, settings)
    assert not any(obj['kind'] == 'PersistentVolumeClaim' for obj in objects)
    assert objects[-1]['spec']['template']['spec']['containers'][0]['image'] == app.image
    assert next(obj for obj in objects if obj['kind'] == 'ApisixRoute')['spec']['ingressClassName'] == 'apisix'
    assert all('persistentVolumeClaim' not in volume for volume in pod['volumes'])


@pytest.mark.parametrize('provider_mode', ['local', 'postman'])
def test_registry_provisioning_journals_writes_encrypts_returned_secret_and_is_reused(tmp_path, provider_mode):
    simulation = os.environ.get('CODECHAT_HARBOR_SIM_URL') if provider_mode == 'postman' else None
    if provider_mode == 'postman' and not simulation:
        pytest.skip('requires local Harbor Postman simulation')
    encryption_key = Fernet.generate_key()
    engine = make_engine('sqlite:///' + str(tmp_path / 'registry.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    subject = uuid4().hex
    digest = identity_digest('issuer', subject)
    project = 'u-' + digest[:24]
    with sessions.begin() as db:
        tenant = Tenant(name='personal', owner_issuer='issuer', owner_subject=subject, project_limit=3)
        db.add(tenant)
        db.flush()
        env = UserEnvironment(issuer='issuer', subject=subject, identity_hash=digest, storage_gib=2, first_tenant_id=tenant.id)
        db.add(env)
        db.flush()
        env_id = env.id
    writes = []
    def provider(request):
        with sessions() as db:
            binding = db.get(UserRegistry, env_id)
            assert binding and binding.encrypted_secret and binding.status.endswith('_inflight')
        body = json.loads(request.content) if request.content else {}
        if request.method == 'POST':
            writes.append(request.url.path)
        if request.url.path == '/api/v2.0/projects' and request.method == 'POST':
            assert body['metadata']['public'] == 'false'
            return httpx.Response(201)
        if request.url.path.startswith('/api/v2.0/projects/'):
            return httpx.Response(200, json={'project_id': 5, 'name': project, 'metadata': {'public': 'false'}})
        if request.url.path == '/api/v2.0/robots':
            assert body['permissions'][0]['namespace'] == project
            assert body['level'] == 'project'
            assert {item['action'] for item in body['permissions'][0]['access']} == {'pull', 'push'}
            assert 'secret' not in body
            return httpx.Response(201, json={'id': 7, 'name': 'robot$' + body['name'], 'secret': 'provider-' + 's' * 48})
        return httpx.Response(501)
    options = {'base_url': simulation, 'trust_env': False} if simulation else {'base_url': 'http://harbor.test', 'transport': httpx.MockTransport(provider)}
    with httpx.Client(**options) as client:
        provision_registry(sessions, env_id, client, encryption_key.decode())
        provision_registry(sessions, env_id, client, encryption_key.decode())
    assert simulation or len(writes) == 2
    with sessions.begin() as db:
        binding = db.get(UserRegistry, env_id)
        assert binding.status == 'ready' and binding.robot_id > 0
        assert len(Fernet(encryption_key).decrypt(binding.encrypted_secret.encode())) >= 16
        binding.status = 'robot_inflight'
    with httpx.Client(base_url='http://harbor.test', transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(RegistryError):
            provision_registry(sessions, env_id, client, encryption_key.decode())
    assert simulation or len(writes) == 2
    engine.dispose()


def test_metric_quantities_keep_units():
    assert quantity('25000000n', cpu=True) == quantity('25m', cpu=True)
    assert quantity('128Mi') == 128 * 1024 ** 2


def test_card_shows_only_database_bound_to_the_confirmed_release():
    database = SimpleNamespace(generation=str(uuid4()), reserved_bytes=128, used_bytes=38, observed_at=None)
    app = SimpleNamespace(status='running', revision='release-1', resources={
        'last_good': {'revision': 'release-1', 'database_secret': database_names(database)[2]},
    })
    assert release_database(app, database) == {'reserved_bytes': 128, 'used_bytes': 38, 'observed_at': None}
    app.revision = 'release-2'
    assert release_database(app, database) is None
    app.status = 'failed'
    app.resources['cleanup'] = {'status': 'complete', 'runtime': 'previous_release'}
    assert release_database(app, database)['used_bytes'] == 38
    database.generation = str(uuid4())
    assert release_database(app, database) is None


@pytest.mark.parametrize('failure', ['build_failed', WRITE_REVIEW_CODE])
def test_retry_requires_a_new_request_and_a_known_write_result(tmp_path, monkeypatch, failure):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from control_plane import applications
    from control_plane.database import get_db
    from control_plane.models import UserApplication, AuditEvent
    from sqlalchemy import select

    engine = make_engine('sqlite:///' + str(tmp_path / 'delivery.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    principal = SimpleNamespace(issuer='issuer', subject='owner')
    with sessions.begin() as db:
        tenant = Tenant(name='personal', owner_issuer='issuer', owner_subject='owner', project_limit=3)
        db.add(tenant)
        db.flush()
        env = UserEnvironment(issuer='issuer', subject='owner', identity_hash=identity_digest('issuer', 'owner'),
                              storage_gib=2, first_tenant_id=tenant.id)
        db.add(env)
        db.flush()
        app = UserApplication(environment_id=env.id, project_path='demo', status='failed',
                              image='registry.test/app@sha256:' + 'a' * 64, error_code=failure,
                              resources={'request_id': 'a' * 64, 'cleanup': {'status': 'complete'}})
        db.add(app)
        db.flush()
        identifier, original_revision = app.id, app.revision
    monkeypatch.setattr(applications, 'get_settings', lambda: Settings(publisher_enabled=True))
    monkeypatch.setattr(applications, 'call_files', lambda *args: httpx.Response(200,
                        json={'entries': [{'name': 'Dockerfile', 'kind': 'file'}]}))
    api = FastAPI()
    api.include_router(applications.create_router(lambda: principal))
    def database():
        with sessions() as db:
            yield db
    api.dependency_overrides[get_db] = database
    client = TestClient(api)
    for _ in range(3):
        result = client.post('/internal/v1/workspace/apps?path=demo', headers={'X-CodeChat-Task-ID': 'a' * 64})
        assert result.status_code == 202 and result.json()['status'] == 'failed'
    with sessions() as db:
        assert db.get(UserApplication, identifier).revision == original_revision
        assert not list(db.scalars(select(AuditEvent)))
    result = client.post('/internal/v1/workspace/apps?path=demo', headers={'X-CodeChat-Task-ID': 'b' * 64})
    if failure == WRITE_REVIEW_CODE:
        assert result.status_code == 409
        for action in ('start', 'stop', 'delete'):
            assert client.post(f'/internal/v1/workspace/apps/{identifier}/{action}').status_code == 409
        with sessions() as db:
            assert db.get(UserApplication, identifier).revision == original_revision
            assert not list(db.scalars(select(AuditEvent)))
    else:
        assert result.status_code == 202 and result.json()['status'] == 'queued'
        with sessions() as db:
            assert db.get(UserApplication, identifier).revision != original_revision
            assert len(list(db.scalars(select(AuditEvent)))) == 1
    engine.dispose()
