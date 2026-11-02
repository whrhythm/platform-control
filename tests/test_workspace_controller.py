from copy import deepcopy
from cryptography.fernet import Fernet
import httpx
import certifi
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from control_plane import workspace_controller
from control_plane.database import make_engine
from control_plane.models import Base, Tenant, UserEnvironment, WorkspaceFileService
from control_plane.settings import Settings
from control_plane.user_environments import identity_digest


def test_service_creation_is_journalled_and_not_repeated(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'services.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    digest = identity_digest('issuer', 'alice')
    with sessions.begin() as db:
        tenant = Tenant(name='alice', owner_issuer='issuer', owner_subject='alice', project_limit=1)
        db.add(tenant)
        db.flush()
        env = UserEnvironment(issuer='issuer', subject='alice', identity_hash=digest, storage_gib=2,
                              first_tenant_id=tenant.id, first_project_id=None)
        db.add(env)
        db.flush()
        env_id = env.id
    class Provider:
        namespace = 'codechat-user-' + digest[:40]
        def __init__(self):
            self.created = {}
            self.posts = 0
        def _request(self, method, path, manifest=None):
            if method == 'POST':
                self.posts += 1
                obj = deepcopy(manifest)
                obj['metadata']['uid'] = f'uid-{self.posts}'
                self.created[path + '/' + obj['metadata']['name']] = obj
                return obj
            return deepcopy(self.created[path])
    provider = Provider()
    client = httpx.Client
    monkeypatch.setattr(workspace_controller.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json={'entries': []})), **kw))
    settings = Settings(model_credential_encryption_key=Fernet.generate_key().decode(),
                        workspace_service_ca=certifi.where(),
                        workspace_service_image='harbor.k3s.local/internal/platform@sha256:' + 'b' * 64)
    assert workspace_controller.prepare_service(sessions, provider, env_id, settings) == 'ready'
    assert workspace_controller.prepare_service(sessions, provider, env_id, settings) == 'ready'
    assert provider.posts == 5
    with sessions() as db:
        service = db.get(WorkspaceFileService, env_id)
        assert service.status == 'ready' and len(service.resources) == 5
        assert all(step['state'] == 'created' for step in service.resources.values())
    engine.dispose()
