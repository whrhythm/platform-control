from control_plane.settings import get_settings
from test_api import headers


def test_test_payment_entitlement_and_isolation(harness, monkeypatch):
    client, token = harness
    monkeypatch.setenv("PLATFORM_TEST_PAYMENTS_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_REQUIRE_PROJECT_ENTITLEMENT", "true")
    get_settings.cache_clear()
    alice = headers(token("alice"))
    bob = headers(token("bob"))
    admin = headers(token("operator", groups=["platform-admins"]))
    tenant = client.post("/v1/tenants", headers=alice, json={"name": "Alice"}).json()["id"]
    projects = f"/v1/tenants/{tenant}/projects"
    orders = f"/v1/tenants/{tenant}/test-orders"
    assert client.post(projects, headers=alice, json={"name": "demo"}).status_code == 403
    assert client.post(orders, headers=bob | {"Idempotency-Key": "one"}).status_code == 404
    order = client.post(orders, headers=alice | {"Idempotency-Key": "one"}).json()
    assert order["status"] == "pending"
    assert client.post(orders, headers=alice | {"Idempotency-Key": "one"}).json()["id"] == order["id"]
    confirm = f"/v1/admin/tenants/{tenant}/test-orders/{order['id']}/confirm"
    assert client.post(confirm, headers=alice).status_code == 403
    assert client.post(confirm, headers=admin).json()["status"] == "paid"
    assert client.post(confirm, headers=admin).json()["status"] == "paid"
    assert client.get(f"{orders}/{order['id']}", headers=bob).status_code == 404
    assert client.post(projects, headers=alice, json={"name": "demo"}).status_code == 201
    assert client.post(projects, headers=bob, json={"name": "stolen"}).status_code == 404
    monkeypatch.setenv("PLATFORM_TEST_PAYMENTS_ENABLED", "false")
    get_settings.cache_clear()
    assert client.post(projects, headers=alice, json={"name": "another"}).status_code == 403


def test_test_payment_is_disabled_by_default(harness):
    client, token = harness
    alice = headers(token())
    tenant = client.post("/v1/tenants", headers=alice, json={"name": "Alice"}).json()["id"]
    assert client.post(f"/v1/tenants/{tenant}/test-orders", headers=alice | {"Idempotency-Key": "one"}).status_code == 404
