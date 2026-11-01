import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from control_plane.auth import jwks_client
from control_plane.database import get_db, make_engine
from control_plane.main import app
from control_plane.models import Base
from control_plane.settings import get_settings


@pytest.fixture
def harness(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})

    class JwksHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/jwks":
                self.send_error(404)
                return
            response = json.dumps({"keys": [public_jwk]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), JwksHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PLATFORM_OIDC_ISSUER", "https://identity.example.test/application/o/platform/")
    monkeypatch.setenv("PLATFORM_OIDC_AUDIENCE", "platform-api")
    monkeypatch.setenv("PLATFORM_OIDC_JWKS_URL", f"http://127.0.0.1:{server.server_port}/jwks")
    monkeypatch.setenv("PLATFORM_DEFAULT_PROJECT_LIMIT", "2")
    get_settings.cache_clear()
    jwks_client.cache_clear()

    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    def test_db():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = test_db

    def token(subject="alice", **override):
        claims = {
            "iss": "https://identity.example.test/application/o/platform/",
            "aud": "platform-api",
            "sub": subject,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "scope": "openid platform-api",
        }
        claims.update(override)
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})

    client = TestClient(app)
    client.session_factory = sessions
    yield client, token
    app.dependency_overrides.clear()
    engine.dispose()
    server.shutdown()
    server.server_close()
    thread.join()
    get_settings.cache_clear()
    jwks_client.cache_clear()


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_authentication_checks_real_jwks_and_claims(harness):
    client, token = harness
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers=headers(token())).json()["subject"] == "alice"
    assert client.get("/v1/me", headers=headers(token(aud="another-api"))).status_code == 401
    assert client.get("/v1/me", headers=headers(token(iss="https://wrong.example"))).status_code == 401
    assert client.get("/v1/me", headers=headers(token(exp=int(time.time()) - 1))).status_code == 401
    assert client.get("/v1/me", headers=headers("invalid-token")).status_code == 401


def test_tenant_project_isolation_and_quotas(harness):
    client, token = harness
    alice, bob = headers(token("alice")), headers(token("bob"))
    tenant = client.post("/v1/tenants", headers=alice, json={"name": "Alice"})
    assert tenant.status_code == 201
    tenant_id = tenant.json()["id"]
    assert client.post("/v1/tenants", headers=alice, json={"name": "Another"}).status_code == 409
    assert client.get("/v1/tenants", headers=bob).json() == []
    assert client.get(f"/v1/tenants/{tenant_id}/projects", headers=bob).status_code == 404
    assert client.post(f"/v1/tenants/{tenant_id}/projects", headers=bob, json={"name": "stolen"}).status_code == 404

    first = client.post(f"/v1/tenants/{tenant_id}/projects", headers=alice, json={"name": "first"})
    assert first.status_code == 201
    assert first.json()["status"] == "draft"
    project_id = first.json()["id"]
    assert client.get(f"/v1/tenants/{tenant_id}/projects/{project_id}", headers=bob).status_code == 404
    assert client.post(f"/v1/tenants/{tenant_id}/projects", headers=alice, json={"name": "first"}).status_code == 409
    assert client.post(f"/v1/tenants/{tenant_id}/projects", headers=alice, json={"name": "second"}).status_code == 201
    assert client.post(f"/v1/tenants/{tenant_id}/projects", headers=alice, json={"name": "third"}).status_code == 409
    assert len(client.get(f"/v1/tenants/{tenant_id}/projects", headers=alice).json()) == 2


def test_rejects_client_supplied_tenant_and_role(harness):
    client, token = harness
    alice = headers(token())
    assert client.post("/v1/tenants", headers=alice, json={"name": "test", "role": "admin"}).status_code == 422
    tenant_id = client.post("/v1/tenants", headers=alice, json={"name": "test"}).json()["id"]
    path = f"/v1/tenants/{tenant_id}/projects"
    assert client.post(path, headers=alice, json={"name": "../bad"}).status_code == 422
    assert client.post(path, headers=alice, json={"name": "ok", "tenant_id": "other"}).status_code == 422


def test_missing_oidc_config_fails_closed(harness, monkeypatch):
    client, token = harness
    monkeypatch.setenv("PLATFORM_OIDC_ISSUER", "")
    get_settings.cache_clear()
    assert client.get("/v1/me", headers=headers(token())).status_code == 503


def test_personal_tenant_bootstrap_is_idempotent_and_scoped(harness):
    client, token = harness
    endpoint = "/v1/me/personal-tenant"
    assert client.post(endpoint).status_code == 401
    alice, bob = headers(token("alice")), headers(token("bob"))
    first = client.post(endpoint, headers=alice)
    assert first.status_code == 200
    assert client.post(endpoint, headers=alice).json()["id"] == first.json()["id"]
    other = client.post(endpoint, headers=bob)
    assert other.status_code == 200
    assert other.json()["id"] != first.json()["id"]
    assert client.get(f"/v1/tenants/{first.json()['id']}/projects", headers=bob).status_code == 404
    assert len(client.get(f"/v1/tenants/{first.json()['id']}/audit", headers=alice).json()) == 1
