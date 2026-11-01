from control_plane.worker import plan_one
from test_api import headers


def setup_tenant(client, token):
    auth = headers(token("alice"))
    tenant_id = client.post("/v1/tenants", headers=auth, json={"name": "Alice"}).json()["id"]
    path = f"/v1/tenants/{tenant_id}"
    project_id = client.post(path + "/projects", headers=auth, json={"name": "one"}).json()["id"]
    return auth, path, project_id


def request(client, path, project_id, auth, body, key="request-key-0001"):
    return client.post(f"{path}/projects/{project_id}/resources", headers={**auth, "Idempotency-Key": key}, json=body)


def test_scope_and_untrusted_admin_headers(harness):
    client, token = harness
    assert client.get("/v1/me", headers=headers(token(scope="openid"))).status_code == 401
    assert client.get("/v1/admin/tenants", headers={**headers(token()), "X-Role": "platform-admins"}).status_code == 403
    assert client.get("/v1/admin/tenants", headers=headers(token(groups="platform-admins"))).status_code == 401
    assert client.get("/v1/admin/tenants", headers=headers(token(groups=["platform-admins"]))).status_code == 200


def test_membership_roles_and_revocation(harness):
    client, token = harness
    alice, path, project_id = setup_tenant(client, token)
    bob = headers(token("bob"))
    assert client.put(path + "/members", headers=alice, json={"subject": "bob", "role": "viewer"}).status_code == 200
    assert client.get(path + "/projects", headers=bob).status_code == 200
    assert request(client, path, project_id, bob, {"kind": "workspace", "name": "dev"}).status_code == 403
    assert client.put(path + "/members", headers=bob, json={"subject": "bob", "role": "admin"}).status_code == 403
    assert client.put(path + "/members", headers=alice, json={"subject": "bob", "role": "developer"}).status_code == 200
    assert request(client, path, project_id, bob, {"kind": "workspace", "name": "dev"}).status_code == 201
    assert client.delete(path + "/members/alice", headers=alice).status_code == 409
    assert client.delete(path + "/members/bob", headers=alice).status_code == 204
    assert client.get(path + "/projects", headers=bob).status_code == 404
    assert client.get(path + "/audit", headers=alice).json()


def test_resource_idempotency_quota_plan_and_cancellation(harness):
    client, token = harness
    auth, path, project_id = setup_tenant(client, token)
    body = {"kind": "workspace", "name": "dev", "storage_gib": 40}
    result = request(client, path, project_id, auth, body)
    assert result.status_code == 201
    resource_id = result.json()["id"]
    assert result.json()["status"] == "requested"
    assert request(client, path, project_id, auth, body).json()["id"] == resource_id
    assert request(client, path, project_id, auth, {**body, "name": "different"}).status_code == 409
    assert request(client, path, project_id, auth, {"kind": "bucket", "name": "files"}, "another-request").status_code == 409
    assert client.get(path + "/quota", headers=auth).json()["reserved"]["storage_gib"] == 40
    assert plan_one(client.session_factory)
    assert not plan_one(client.session_factory)
    operation = client.get(path + "/operations", headers=auth).json()[0]
    assert operation["status"] == "planned"
    assert operation["plan"]["executable"] is False
    manifests = operation["plan"]["kubernetes"]
    pod = next(item for item in manifests if item["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert pod["runtimeClassName"] == "kata-qemu-runtime-rs"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"]
    assert "hostPath" not in str(manifests)
    assert "Secret" not in {item["kind"] for item in manifests}
    assert client.delete(path + "/resources/" + resource_id, headers=auth).json()["status"] == "cancelled"
    assert client.get(path + "/quota", headers=auth).json()["reserved"]["storage_gib"] == 0
    assert client.get(path + "/operations", headers=auth).json()[0]["plan"] is None


def test_cross_tenant_and_project_resources_cannot_be_referenced(harness):
    client, token = harness
    alice, path, project_id = setup_tenant(client, token)
    workspace = request(client, path, project_id, alice, {"kind": "workspace", "name": "dev"}).json()
    second_id = client.post(path + "/projects", headers=alice, json={"name": "two"}).json()["id"]
    assert request(client, path, second_id, alice, {"kind": "agent", "name": "agent", "workspace_id": workspace["id"]}, "cross-project-key").status_code == 404
    assert request(client, path, project_id, headers(token("bob")), {"kind": "workspace", "name": "dev"}).status_code == 404
    assert client.get(path + "/operations", headers=headers(token("bob"))).status_code == 404
    assert client.delete(path + "/resources/" + workspace["id"], headers=headers(token("bob"))).status_code == 404
    agent = request(client, path, project_id, alice, {"kind": "agent", "name": "agent", "workspace_id": workspace["id"]}, "valid-agent-key")
    assert agent.status_code == 201
    assert request(client, path, project_id, alice, {"kind": "agent", "name": "agent-two", "workspace_id": workspace["id"]}, "second-agent-key").status_code == 409
    assert client.delete(path + "/resources/" + workspace["id"], headers=alice).status_code == 409


def test_deployments_validate_registry_digest_and_reject_arbitrary_options(harness):
    client, token = harness
    auth, path, project_id = setup_tenant(client, token)
    base = {"kind": "deployment", "name": "site", "image": "harbor.k3s.local/internal/site@sha256:" + "a" * 64}
    assert request(client, path, project_id, auth, {**base, "image": "attacker.example/site@sha256:" + "a" * 64}).status_code == 422
    assert request(client, path, project_id, auth, {**base, "image": "harbor.k3s.local/internal/site:latest"}).status_code == 422
    assert request(client, path, project_id, auth, {**base, "hostPath": "/"}).status_code == 422
    assert request(client, path, project_id, auth, base).status_code == 201
    plan_one(client.session_factory)
    plan = client.get(path + "/operations", headers=auth).json()[0]["plan"]
    assert plan["proposed_hostname"] is None
    assert all(item["kind"] != "Ingress" for item in plan["kubernetes"])


def test_platform_admin_quota_cannot_be_lower_than_reservations(harness):
    client, token = harness
    auth, path, project_id = setup_tenant(client, token)
    request(client, path, project_id, auth, {"kind": "workspace", "name": "dev", "storage_gib": 10})
    body = {"project_limit": 3, "cpu_millicores": 4000, "memory_mib": 8192, "storage_gib": 5, "resource_limit": 20}
    admin_path = path.replace("/v1/tenants/", "/v1/admin/tenants/") + "/quota"
    assert client.put(admin_path, headers=auth, json=body).status_code == 403
    admin = headers(token("operator", groups=["platform-admins"]))
    assert client.put(admin_path, headers=admin, json=body).status_code == 409
    assert client.put(admin_path, headers=admin, json={**body, "storage_gib": 20}).status_code == 200
    assert client.get(path + "/projects", headers=admin).status_code == 404


def test_project_plan_reclaim_is_atomic_authorized_and_idempotent(harness):
    from sqlalchemy import select
    from control_plane.models import Resource

    client, token = harness
    auth, path, project_id = setup_tenant(client, token)
    workspace = request(client, path, project_id, auth, {"kind": "workspace", "name": "dev"}, "workspace-key").json()
    agent = request(client, path, project_id, auth, {"kind": "agent", "name": "helper", "workspace_id": workspace["id"]}, "agent-key-001").json()
    assert plan_one(client.session_factory)
    endpoint = f"{path}/projects/{project_id}/cancel-plans"
    assert client.post(endpoint, headers=headers(token("bob"))).status_code == 404
    assert client.put(path + "/members", headers=auth, json={"subject": "bob", "role": "developer"}).status_code == 200
    assert client.post(endpoint, headers=headers(token("bob"))).status_code == 403
    with client.session_factory() as db:
        db.scalar(select(Resource).where(Resource.id == agent["id"])).status = "ready"
        db.commit()
    assert client.post(endpoint, headers=auth).status_code == 409
    assert client.get(path + "/quota", headers=auth).json()["reserved"]["storage_gib"] == 5
    with client.session_factory() as db:
        db.scalar(select(Resource).where(Resource.id == agent["id"])).status = "requested"
        db.commit()
    result = client.post(endpoint, headers=auth)
    assert result.status_code == 200
    assert result.json() == {"project_id": project_id, "cancelled_count": 2, "mode": "plans_only", "infrastructure_deleted": False}
    assert client.get(path + "/quota", headers=auth).json()["reserved"]["storage_gib"] == 0
    assert all(item["status"] == "cancelled" and item["plan"] is None for item in client.get(path + "/operations", headers=auth).json())
    assert client.post(endpoint, headers=auth).json()["cancelled_count"] == 0
