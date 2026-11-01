"""Mirror pinned LibreChat prerequisites into the internal registry on edge.

Run on the cluster host; Harbor credentials remain in a temporary Docker config.
"""
import base64
import json
import os
import subprocess
import tempfile


IMAGES = {
    "registry.librechat.ai/librechat-ai/librechat:v0.8.9-rc1": "harbor.k3s.local/internal/librechat:v0.8.9-rc1",
    "mongo:8.0.20": "harbor.k3s.local/internal/librechat-mongo:8.0.20",
}


def main():
    secret = json.loads(subprocess.check_output(["kubectl", "-n", "harbor", "get", "secret", "harbor-secrets", "-o", "json"], text=True))
    password = base64.b64decode(secret["data"]["HARBOR_ADMIN_PASSWORD"]).decode()
    with tempfile.TemporaryDirectory(prefix="librechat-harbor-auth-") as config:
        os.chmod(config, 0o700)
        env = {**os.environ, "DOCKER_CONFIG": config}
        login = subprocess.run(["docker", "login", "harbor.k3s.local", "--username", "admin", "--password-stdin"], input=password, text=True, capture_output=True, env=env)
        if login.returncode:
            raise RuntimeError("Harbor login failed")
        for source, destination in IMAGES.items():
            subprocess.run(["docker", "pull", source], check=True, env=env)
            subprocess.run(["docker", "tag", source, destination], check=True, env=env)
            subprocess.run(["docker", "push", destination], check=True, env=env)
            info = json.loads(subprocess.check_output(["docker", "image", "inspect", destination], text=True, env=env))[0]
            print(json.dumps({"source": source, "digest": info["RepoDigests"]}), flush=True)


if __name__ == "__main__":
    main()
