import json
import os
import queue
import subprocess
import sys
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane.sidecar import create_app

TOKEN = "test-local-token-" + "x" * 40
HEADERS = {"X-Desktop-Token": TOKEN, "Origin": "tauri://localhost"}


def test_local_bridge_rejects_cross_origin_rebinding_and_missing_token():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"path": request.url.path}))
    app = create_app(TOKEN, "https://platform.example.test", ["tauri://localhost"], transport=transport)
    with TestClient(app, base_url="http://127.0.0.1:12345") as client:
        assert client.get("/v1/me").status_code == 401
        assert client.get("/v1/me", headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
        assert client.get("/v1/me", headers={**HEADERS, "Host": "evil.example"}).status_code == 403
        assert client.get("/v1/me", headers=HEADERS).json() == {"path": "/v1/me"}
        assert client.get("/arbitrary-upstream", headers=HEADERS).status_code == 404
        assert client.post("/v1/tenants", headers=HEADERS, content=b"x" * (1024 * 1024 + 1)).status_code == 413


def test_bridge_forwards_only_platform_auth_and_preserves_denials():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(403, json={"detail": "Insufficient tenant role"}, headers={"Set-Cookie": "secret=value"})

    bridge = create_app(TOKEN, "https://platform.example.test", ["tauri://localhost"], transport=httpx.MockTransport(handler))
    with TestClient(bridge, base_url="http://127.0.0.1:12345") as client:
        response = client.get("/v1/me", headers={**HEADERS, "Authorization": "Bearer test-access-token", "Cookie": "private=value", "X-Tenant": "attacker"})
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert requests[0].headers["authorization"] == "Bearer test-access-token"
    assert "x-desktop-token" not in requests[0].headers
    assert "cookie" not in requests[0].headers
    assert "x-tenant" not in requests[0].headers


@pytest.mark.parametrize("upstream", ["http://remote.example", "https://user:password@example.test", "https://example.test/path", "https://example.test?query=1"])
def test_bridge_requires_fixed_safe_upstream(upstream):
    with pytest.raises(ValueError):
        create_app(TOKEN, upstream, ["tauri://localhost"])


def test_real_sidecar_binds_loopback_and_exits_when_parent_pipe_closes():
    binary = os.environ.get("PLATFORM_TEST_SIDECAR_BINARY")
    command = [binary] if binary else [sys.executable, "-m", "control_plane.sidecar"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        process.stdin.write(json.dumps({"token": TOKEN, "upstream": "http://127.0.0.1:8088", "origins": ["tauri://localhost"]}) + "\n")
        process.stdin.flush()
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
        info = json.loads(ready.get(timeout=15))
        assert info["protocol"] == 1
        assert "token" not in info
        with httpx.Client(trust_env=False) as client:
            url = f"http://127.0.0.1:{info['port']}/healthz"
            assert client.get(url).status_code == 401
            assert client.get(url, headers=HEADERS).status_code == 200
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
