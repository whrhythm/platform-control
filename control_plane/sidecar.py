"""Desktop-only local HTTP bridge. No cluster/provider credentials or business RBAC."""
import asyncio
import hmac
import json
import os
import socket
import ssl
import sys
import threading
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response


def create_app(token: str, upstream: str, origins: list[str], transport=None) -> FastAPI:
    parsed = urlsplit(upstream)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Upstream must be a bare trusted origin")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
        raise ValueError("HTTPS upstream required (loopback development excepted)")
    if len(token) < 32:
        raise ValueError("Local token is too short")
    bridge = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    bridge.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["X-Desktop-Token", "Authorization", "Content-Type", "Idempotency-Key"])

    @bridge.middleware("http")
    async def protect(request: Request, call_next):
        if request.url.hostname != "127.0.0.1":
            return JSONResponse({"detail": "Invalid local host"}, 403)
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return JSONResponse({"detail": "Origin denied"}, 403)
        if request.method != "OPTIONS" and not hmac.compare_digest(request.headers.get("x-desktop-token", ""), token):
            return JSONResponse({"detail": "Local session required"}, 401)
        return await call_next(request)

    @bridge.get("/healthz")
    async def health():
        return {"status": "ok", "protocol": 1}

    @bridge.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy(path: str, request: Request):
        # A single configured upstream; paths cannot select another destination.
        # Never forward local tokens, cookies, Host, proxy or hop-by-hop headers.
        if any(segment in {".", ".."} for segment in path.split("/")) or "\\" in path:
            return JSONResponse({"detail": "Invalid API path"}, 400)
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1024 * 1024:
                return JSONResponse({"detail": "Request too large"}, 413)
            chunks.append(chunk)
        headers = {key: value for key, value in request.headers.items() if key in {"authorization", "content-type", "idempotency-key", "accept"}}
        try:
            tls = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, transport=transport, trust_env=False, verify=tls) as client:
                response = await client.request(request.method, upstream.rstrip("/") + "/v1/" + path, params=list(request.query_params.multi_items()), headers=headers, content=b"".join(chunks))
            allowed = {key: value for key, value in response.headers.items() if key in {"content-type", "www-authenticate", "retry-after"}}
            return Response(response.content, status_code=response.status_code, headers=allowed)
        except httpx.HTTPError:
            return JSONResponse({"detail": "Platform service unavailable"}, 502)

    return bridge


def main():
    # The GUI writes this secret over an inherited pipe, never command arguments.
    config = json.loads(sys.stdin.readline())
    app = create_app(config["token"], config["upstream"], config["origins"])

    def parent_lifetime():
        # EOF also covers abrupt GUI termination, including Windows. This bridge
        # never spawns child processes, so there is no process tree to orphan.
        while sys.stdin.buffer.read(1):
            pass
        os._exit(0)

    threading.Thread(target=parent_lifetime, daemon=True).start()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical"))

    async def serve():
        async def announce():
            while not server.started:
                await asyncio.sleep(0.02)
            print(json.dumps({"port": port, "protocol": 1}), flush=True)
        ready = asyncio.create_task(announce())
        await server.serve(sockets=[sock])
        ready.cancel()
    asyncio.run(serve())


if __name__ == "__main__":
    main()
