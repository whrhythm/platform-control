"""Run on edge after docker load. Credentials are transient and never printed."""
import base64
import json
import os
import subprocess
import tempfile

TAG = os.environ.get("PLATFORM_IMAGE_TAG", "0.1.0-20261031.2")
LOCAL = "platform-control:" + TAG
DESTINATION = "harbor.k3s.local/internal/platform-control:" + TAG


def main():
    secret = json.loads(subprocess.check_output(["kubectl", "-n", "harbor", "get", "secret", "harbor-secrets", "-o", "json"], text=True))
    password = base64.b64decode(secret["data"]["HARBOR_ADMIN_PASSWORD"]).decode()
    with tempfile.TemporaryDirectory(prefix="platform-harbor-auth-") as config:
        os.chmod(config, 0o700)
        env = {**os.environ, "DOCKER_CONFIG": config}
        result = subprocess.run(["docker", "login", "harbor.k3s.local", "--username", "admin", "--password-stdin"], input=password, text=True, capture_output=True, env=env)
        if result.returncode:
            raise RuntimeError("Harbor login failed: " + result.stderr.replace(password, "[redacted]"))
        subprocess.run(["docker", "tag", LOCAL, DESTINATION], check=True)
        pushed = subprocess.run(["docker", "push", DESTINATION], env=env, capture_output=True, text=True)
        if pushed.returncode:
            raise RuntimeError("Harbor push failed: " + pushed.stderr.replace(password, "[redacted]")[-2000:])
        info = json.loads(subprocess.check_output(["docker", "image", "inspect", DESTINATION], text=True))[0]
        print(json.dumps({"image": DESTINATION, "repo_digests": info["RepoDigests"], "image_id": info["Id"]}))


if __name__ == "__main__":
    main()
