"""Prepare non-Git LibreChat credentials and internal CA; never rotate an existing secret."""
import base64
import json
import os
import secrets
import subprocess


KUBECTL = os.environ.get("KUBECTL", "kubectl")
SERVER = os.environ.get("KUBE_SERVER", "https://192.168.10.30:6443")


def kube(*args, data=None):
    result = subprocess.run([KUBECTL, "--server", SERVER, *args], input=data, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("kubectl failed: " + result.stderr[-1000:])
    return result.stdout


def encode(value):
    return base64.b64encode(value.encode()).decode()


def main():
    existing = kube("-n", "librechat", "get", "secret", "librechat-secrets", "--ignore-not-found", "-o", "json")
    required = {"mongo-password", "MONGO_URI", "CREDS_KEY", "CREDS_IV", "JWT_SECRET", "JWT_REFRESH_SECRET", "OPENID_SESSION_SECRET"}
    if existing.strip():
        if not required <= set(json.loads(existing).get("data", {})):
            raise RuntimeError("Existing LibreChat secret is incomplete; refusing to rotate it")
        print("Existing LibreChat credentials retained")
    else:
        password = secrets.token_hex(32)
        values = {
            "mongo-password": password,
            "MONGO_URI": f"mongodb://root:{password}@librechat-mongo:27017/LibreChat?authSource=admin",
            "CREDS_KEY": secrets.token_hex(32),
            "CREDS_IV": secrets.token_hex(16),
            "JWT_SECRET": secrets.token_hex(32),
            "JWT_REFRESH_SECRET": secrets.token_hex(32),
            "OPENID_SESSION_SECRET": secrets.token_hex(32),
        }
        manifest = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "librechat-secrets", "namespace": "librechat"}, "type": "Opaque", "data": {k: encode(v) for k, v in values.items()}}
        kube("create", "-f", "-", data=json.dumps(manifest))
        print("Created LibreChat credentials")
    source = json.loads(kube("-n", "platform-system", "get", "configmap", "platform-ca", "-o", "json"))
    certificate = source["data"]["internal-ca.crt"]
    manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "librechat-ca", "namespace": "librechat"}, "data": {"internal-ca.crt": certificate}}
    kube("apply", "-f", "-", data=json.dumps(manifest))
    print("Installed internal CA for LibreChat")


if __name__ == "__main__":
    main()
