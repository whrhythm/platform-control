from control_plane.worker import plan_one
from test_resources import request, setup_tenant
import pytest
from sqlalchemy.exc import IntegrityError
from control_plane.models import Resource
from test_api import headers


def test_project_database_and_bucket_plans_are_separate_and_have_no_secrets(harness):
    client, token = harness
    auth, path, project = setup_tenant(client, token)
    for key, body in enumerate([
        {"kind": "database", "name": "db", "environment": "development"},
        {"kind": "database", "name": "db-prod", "environment": "production"},
        {"kind": "bucket", "name": "files"},
    ]):
        assert request(client, path, project, auth, body, f"resource-key-{key}").status_code == 201
    while plan_one(client.session_factory):
        pass
    operations = client.get(path + "/operations", headers=auth).json()
    databases = [op["plan"]["database"] for op in operations if "database" in op["plan"]]
    assert len(databases) == 2
    assert databases[0]["role"] != databases[1]["role"]
    bucket = next(op["plan"]["bucket"] for op in operations if "bucket" in op["plan"])
    for statement in bucket["policy"]["Statement"]:
        assert all(bucket["name"] in arn for arn in statement["Resource"])
        assert "s3:*" not in statement["Action"]
    for operation in operations:
        assert operation["status"] == "planned"
        assert operation["plan"]["executable"] is False
        assert "password" not in str(operation["plan"].get("spec", {}))


def test_database_rejects_resource_with_mismatched_project_tenant(harness):
    client, token = harness
    auth, path, project = setup_tenant(client, token)
    other_tenant = client.post("/v1/tenants", headers=headers(token("bob")), json={"name": "Bob"}).json()["id"]
    with client.session_factory() as db:
        db.add(Resource(tenant_id=other_tenant, project_id=project, kind="workspace", name="forged", spec={}, idempotency_key="forged-key", request_hash="a" * 64))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
