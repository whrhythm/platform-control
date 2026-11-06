"""Transfer a minimal build context, verify it and build/publish on edge."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile

from cluster_transport import HOST, SOCKET, remote


def main():
    root = Path(__file__).resolve().parents[1]
    archive = root / "release" / "platform-build-context.tar.gz"
    files = [root / name for name in ["Dockerfile.prebuilt-web", "Dockerfile.agent", "Dockerfile.orchestrator", "requirements.runtime.txt", "pyproject.toml", "uv.lock", "alembic.ini", "scripts/push_on_host.py"]]
    files += [path for folder in ["control_plane", "migrations"] for path in (root / folder).rglob("*.py")]
    files += [path for path in (root / 'control_plane/templates').rglob('*')
              if path.is_file() and (path.suffix in ('.html', '.md') or path.name == 'Dockerfile')]
    files += [path for path in (root / "web/dist").rglob("*") if path.is_file()]
    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
        for path in sorted(files):
            tar.add(path, arcname=str(path.relative_to(root)), recursive=False)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    tag = os.environ.get("PLATFORM_IMAGE_TAG", "0.1.0-20261031.2")
    if not tag or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in tag):
        raise ValueError("Invalid image tag")
    destination = "/home/edge/platform-releases/" + tag
    remote(["mkdir", "-p", destination])
    with archive.open("rb") as source:
        subprocess.run(["ssh", "-S", SOCKET, HOST, f"cat > {destination}/platform-build-context.tar.gz"], stdin=source, check=True)
    checked = remote(["sha256sum", destination + "/platform-build-context.tar.gz"]).split()[0]
    if checked != digest:
        raise RuntimeError("Remote build context checksum mismatch")
    remote(["mkdir", "-p", destination + "/context"])
    remote(["tar", "-xzf", destination + "/platform-build-context.tar.gz", "-C", destination + "/context"])
    dockerfile = 'Dockerfile.agent' if os.environ.get('PLATFORM_BUILD_TARGET') == 'agent' else 'Dockerfile.prebuilt-web'
    if os.environ.get('PLATFORM_BUILD_TARGET') == 'orchestrator':
        dockerfile = 'Dockerfile.orchestrator'
    command = "cd " + destination + "/context && docker build --network=host --provenance=false -f " + dockerfile + " -t platform-control:" + tag + " . && PLATFORM_IMAGE_TAG=" + tag + " python3 scripts/push_on_host.py > " + destination + "/published.json"
    # Keep the actual build alive if the operator's slow SSH link drops.
    launch = "nohup /bin/sh -ec " + shlex.quote(command) + " > " + destination + "/build.log 2>&1 < /dev/null & echo $!"
    pid = remote(["/bin/sh", "-c", launch]).strip()
    print(json.dumps({"context_bytes": archive.stat().st_size, "sha256": digest, "build_pid": pid, "log": destination + "/build.log"}))


if __name__ == "__main__":
    main()
