import hashlib
import json

from test_api import headers


def identity_hash(subject):
    issuer = "https://identity.example.test/application/o/platform/"
    return hashlib.sha256(json.dumps([issuer, subject], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def environment_url(tenant_id, project_id):
    return f"/v1/tenants/{tenant_id}/projects/{project_id}/environment"


def setup_project(client, token, user="alice", project_name="one"):
    auth = headers(token(user))
    tenant_id = client.post("/v1/tenants", headers=auth, json={"name": user.title()}).json()["id"]
    project_id = client.post(f"/v1/tenants/{tenant_id}/projects", headers=auth, json={"name": project_name}).json()["id"]
    return auth, tenant_id, project_id


def test_environment_requires_authentication_and_membership(harness):
    client, token = harness
    assert client.post(environment_url("0" * 36, "0" * 36)).status_code == 401
    assert client.get(environment_url("0" * 36, "0" * 36)).status_code == 401
    _auth, tenant_id, project_id = setup_project(client, token)
    stranger = headers(token("stranger"))
    assert client.post(environment_url(tenant_id, project_id), headers=stranger).status_code == 404
    assert client.get(environment_url(tenant_id, project_id), headers=stranger).status_code == 404


def test_first_request_creates_plan_only_environment(harness):
    client, token = harness
    alice, tenant_id, project_id = setup_project(client, token)
    response = client.post(environment_url(tenant_id, project_id), headers=alice)
    assert response.status_code == 200
    body = response.json()
    digest = identity_hash("alice")
    assert body["identity_hash"] == digest
    assert body["namespace"] == "codechat-user-" + digest[:40]
    assert body["storage_gib"] == 2
    assert body["status"] == "requested"
    assert body["mode"] == "plan"
    assert body["executable"] is False
    assert body["runtime_requirement"]["runtime_class"] == "kata-qemu-runtime-rs"
    assert body["first_project_id"] == project_id
    kinds = [obj["kind"] for obj in body["plan"]]
    assert sorted(kinds) == ["Namespace", "NetworkPolicy", "PersistentVolumeClaim", "ResourceQuota"]
    pvc = next(obj for obj in body["plan"] if obj["kind"] == "PersistentVolumeClaim")
    assert pvc["spec"]["storageClassName"] == "codechat-workspace-retain"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "2Gi"
    deny = next(obj for obj in body["plan"] if obj["kind"] == "NetworkPolicy")
    assert deny["spec"]["ingress"] == [] and deny["spec"]["egress"] == []
    assert body["blockers"]
    plan_text = json.dumps(body["plan"])
    assert "alice" not in plan_text
    audit = client.get(f"/v1/tenants/{tenant_id}/audit", headers=alice).json()
    assert [event["action"] for event in audit].count("environment.create") == 1


def test_repeated_request_is_idempotent_and_get_returns_same_record(harness):
    client, token = harness
    alice, tenant_id, project_id = setup_project(client, token)
    url = environment_url(tenant_id, project_id)
    first = client.post(url, headers=alice).json()
    second = client.post(url, headers=alice)
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]
    assert client.get(url, headers=alice).json()["id"] == first["id"]
    audit = client.get(f"/v1/tenants/{tenant_id}/audit", headers=alice).json()
    assert [event["action"] for event in audit].count("environment.create") == 1


def test_personal_environment_lookup_uses_verified_identity(harness):
    client, token = harness
    assert client.get('/v1/me/environment').status_code == 401
    alice, tenant_id, project_id = setup_project(client, token)
    bob = headers(token('bob'))
    assert client.get('/v1/me/environment', headers=alice).status_code == 404
    environment = client.post(environment_url(tenant_id, project_id), headers=alice).json()
    response = client.get('/v1/me/environment', headers=alice)
    assert response.status_code == 200
    assert response.json()['id'] == environment['id']
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/v1/me/environment', headers=bob).status_code == 404


def test_existing_project_environment_is_reused_on_login(harness):
    from control_plane.auth import Principal
    from control_plane.main import ensure_personal_environment

    client, token = harness
    alice, tenant_id, project_id = setup_project(client, token)
    original = client.post(environment_url(tenant_id, project_id), headers=alice).json()
    with client.session_factory() as db:
        principal = Principal(issuer='https://identity.example.test/application/o/platform/', subject='alice')
        result = ensure_personal_environment(principal, tenant_id, db)
        assert result.id == original['id']
        assert result.first_project_id == project_id


def test_environment_is_scoped_to_the_requesting_user(harness):
    client, token = harness
    alice, tenant_id, project_id = setup_project(client, token)
    url = environment_url(tenant_id, project_id)
    assert client.put(f"/v1/tenants/{tenant_id}/members", headers=alice, json={"subject": "bob", "role": "developer"}).status_code == 200
    bob = headers(token("bob"))
    env_alice = client.post(url, headers=alice).json()
    env_bob = client.post(url, headers=bob).json()
    assert env_bob["id"] != env_alice["id"]
    assert env_bob["identity_hash"] == identity_hash("bob")
    assert env_bob["namespace"] != env_alice["namespace"]
    assert client.get(url, headers=bob).json()["id"] == env_bob["id"]
    audit = client.get(f"/v1/tenants/{tenant_id}/audit", headers=alice).json()
    assert [event["action"] for event in audit].count("environment.create") == 2


def test_viewer_cannot_request_environment(harness):
    client, token = harness
    alice, tenant_id, project_id = setup_project(client, token)
    url = environment_url(tenant_id, project_id)
    assert client.put(f"/v1/tenants/{tenant_id}/members", headers=alice, json={"subject": "bob", "role": "viewer"}).status_code == 200
    bob = headers(token("bob"))
    assert client.post(url, headers=bob).status_code == 403
    assert client.get(url, headers=bob).status_code == 404


def test_project_outside_tenant_is_rejected(harness):
    client, token = harness
    _auth, tenant_id, project_id = setup_project(client, token, user="bob")
    alice = headers(token("alice"))
    url = environment_url(tenant_id, project_id)
    assert client.post(url, headers=alice).status_code == 404
    assert client.get(url, headers=alice).status_code == 404


def test_client_body_cannot_influence_identity_or_storage(harness):
    client, token = harness
    alice, tenant_id, project_id = setup_project(client, token)
    response = client.post(environment_url(tenant_id, project_id), headers=alice, json={"issuer": "evil", "subject": "evil", "storage_gib": 99, "namespace": "hack"})
    assert response.status_code == 200
    assert response.json()["identity_hash"] == identity_hash("alice")
    assert response.json()["storage_gib"] == 2
