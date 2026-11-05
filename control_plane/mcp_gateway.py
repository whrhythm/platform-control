"""Stateless remote MCP transport: no DB, filesystem, cluster or model credentials."""
import asyncio
import hmac
import ipaddress
import json
import logging
import os
import socket
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .agent_mcp import PublicTransport
from .mcp_catalog import endpoint, safe_schema, fingerprint

app = FastAPI(docs_url=None, openapi_url=None)
slots = asyncio.Semaphore(4)


class Envelope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str = Field(pattern='^(discover|call)$')
    url: str = Field(max_length=500)
    secret: SecretStr = SecretStr('')
    addresses: list[str] = Field(default_factory=list, max_length=32)
    tool: dict = Field(default_factory=dict)
    arguments: dict = Field(default_factory=dict)
    seconds: int = Field(default=60, ge=3, le=120)


async def exchange(body):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from jsonschema import Draft202012Validator
    from referencing import Registry
    url = endpoint(body.url)
    host = urlsplit(url).hostname
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = sorted({value[4][0] for value in infos if ipaddress.ip_address(value[4][0]).version == 4})
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError('non_public_endpoint')
    if body.action == 'call' and (not body.addresses or not set(addresses) <= set(body.addresses)):
        raise ValueError('address_changed')
    server = {'url': url, 'host': host, 'addresses': addresses if body.action == 'discover' else body.addresses}
    async with httpx.AsyncClient(transport=PublicTransport(server, secret=body.secret.get_secret_value()),
                                follow_redirects=False, trust_env=False, timeout=15) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=body.seconds)) as session:
                await session.initialize()
                tools, cursor = [], None
                for _ in range(5):
                    result = await session.list_tools(cursor=cursor)
                    tools.extend(tool.model_dump(by_alias=True, exclude_none=True) for tool in result.tools)
                    if len(tools) > 80 or len(json.dumps(tools)) > 180000:
                        raise ValueError('catalog_too_large')
                    cursor = result.nextCursor
                    if not cursor:
                        break
                if cursor:
                    raise ValueError('catalog_too_large')
                if body.secret.get_secret_value() and body.secret.get_secret_value() in json.dumps(tools):
                    raise ValueError('credential_reflected_in_schema')
                if body.action == 'discover':
                    output = []
                    for tool in tools:
                        schema = tool['inputSchema']
                        supported = safe_schema(schema)
                        if supported:
                            try:
                                Draft202012Validator.check_schema(schema)
                            except Exception:
                                supported = False
                        output.append({**{k: tool.get(k) for k in ('name', 'description', 'inputSchema', 'annotations')},
                                       'fingerprint': fingerprint(tool), 'supported': supported})
                    return {'tools': output, 'addresses': addresses}
                approved = body.tool
                actual = next((t for t in tools if t['name'] == approved.get('name')), None)
                if not actual or (approved.get('fingerprint') and fingerprint(actual) != approved['fingerprint']):
                    return {'error': 'mcp_tools_changed', 'dispatched': False}
                if not safe_schema(approved['inputSchema']):
                    return {'error': 'mcp_schema_unsupported', 'dispatched': False}
                Draft202012Validator(approved['inputSchema'], registry=Registry()).validate(body.arguments)
                # No automatic retry; unknown writes are handled by the task gateway.
                result = await session.call_tool(approved['name'], body.arguments)
                if result.isError:
                    return {'error': 'mcp_tool_failed', 'dispatched': True}
                text = '\n'.join(item.text for item in result.content if item.type == 'text')
                secret = body.secret.get_secret_value()
                if secret:
                    text = text.replace(secret, '[credential redacted]')
                return {'text': text[:12000], 'truncated': len(text) > 12000, 'untrusted_external_data': True, 'dispatched': True}


@app.get('/healthz')
def health():
    return {'ok': True}


@app.post('/exchange')
async def handle(request: Request):
    expected = os.environ.get('PLATFORM_MCP_GATEWAY_TOKEN', '')
    if len(expected) < 32 or not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + expected):
        return JSONResponse({'error': 'unauthorized'}, status_code=403)
    if slots.locked():
        return JSONResponse({'error': 'mcp_busy', 'dispatched': False}, status_code=429)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 65536:
            return JSONResponse({'error': 'request_too_large', 'dispatched': False}, status_code=413)
    try:
        body = Envelope.model_validate_json(data)
        endpoint(body.url)
        token = body.secret.get_secret_value()
        if len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError('invalid_credential')
    except Exception:
        return JSONResponse({'error': 'mcp_request_invalid', 'dispatched': False}, status_code=422)
    async with slots:
        task = asyncio.create_task(asyncio.wait_for(exchange(body), body.seconds))
        try:
            while not task.done():
                await asyncio.wait([task], timeout=0.5)
                if await request.is_disconnected():
                    task.cancel()
                    return JSONResponse({'error': 'mcp_disconnected'}, status_code=503)
            return await task
        except Exception:
            return JSONResponse({'error': 'mcp_transport_failed'}, status_code=503)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    import uvicorn
    logger = logging.getLogger('mcp')
    logger.handlers, logger.propagate = [logging.NullHandler()], False
    uvicorn.run(app, host='0.0.0.0', port=8443, access_log=False, limit_concurrency=12,
                ssl_certfile='/etc/mcp-tls/tls.crt', ssl_keyfile='/etc/mcp-tls/tls.key')
