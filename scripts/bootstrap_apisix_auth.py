"""Provision private Admin API credentials, without exposing their values."""
import json
import secrets
from cluster_transport import remote

name = "apisix-admin-auth"
existing = remote(["kubectl", "-n", "apisix", "get", "secret", name, "--ignore-not-found", "-o", "json"])
if existing.strip():
    secret = json.loads(existing)
    assert {"admin", "viewer"} <= set(secret["data"])
else:
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "apisix"}, "type": "Opaque", "stringData": {"admin": secrets.token_hex(32), "viewer": secrets.token_hex(32)}}
    remote(["kubectl", "create", "-f", "-"], json.dumps(secret), redact=tuple(secret["stringData"].values()))
print("APISIX private management credentials ready (values not printed)")
