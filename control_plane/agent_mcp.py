"""Reviewed, public read-only MCP connectors. No user URLs, commands or credentials."""
import asyncio
import ipaddress
import json
import socket
from datetime import timedelta

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


CATALOG = {
    'deepwiki': {
        'id': 'deepwiki', 'version': 1, 'name': 'DeepWiki · 公开代码文档',
        'description': '查阅公开 GitHub 仓库的文档结构、说明和技术问答。不能读取私有仓库。',
        'url': 'https://mcp.deepwiki.com/mcp', 'host': 'mcp.deepwiki.com',
        'tools': ['read_wiki_structure', 'read_wiki_contents', 'ask_question'],
        'permission': 'public_read', 'approval': 'every_call',
        'data_notice': '仓库名称和查询问题会发送给 DeepWiki。每次调用先在聊天中确认；请勿发送密码、密钥或私有代码。',
    },
}


class RepositoryQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repoName: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')


class QuestionQuery(RepositoryQuery):
    question: str = Field(min_length=1, max_length=2000)


def validate_arguments(server, tool, arguments):
    if server not in CATALOG or tool not in CATALOG[server]['tools']:
        raise ValueError('mcp_tool_not_allowed')
    schema = QuestionQuery if tool == 'ask_question' else RepositoryQuery
    return schema.model_validate(arguments).model_dump()


def enabled_servers(db, settings):
    from .ai_policy import effective_policy
    return effective_policy(db, settings).get('mcp_servers', [])


def public_catalog(db, settings):
    enabled = enabled_servers(db, settings)
    return {'servers': [{**{k: v for k, v in server.items() if k != 'host'}, 'enabled': key in enabled}
                        for key, server in CATALOG.items()]}


def validate_selection(db, settings, selected):
    if len(selected) != len(set(selected)) or not set(selected) <= set(enabled_servers(db, settings)):
        raise HTTPException(409, 'agent_mcp_selection_unavailable')


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream, maximum):
        self.stream, self.maximum = stream, maximum

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > self.maximum:
                raise ValueError('mcp_response_too_large')
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PublicTransport(httpx.AsyncBaseTransport):
    """Pin each connection to a validated public address; preserve TLS SNI/verification.

    Checking DNS then letting HTTP resolve again would permit DNS rebinding.
    No proxies, redirects, alternate ports, URLs, credentials or compressed bodies.
    """
    def __init__(self, server, maximum=262144):
        self.server, self.maximum = server, maximum
        self.transport = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if (str(request.url) != self.server['url'] or request.method not in ('POST', 'GET', 'DELETE')
                or request.headers.get('authorization') or request.headers.get('cookie')):
            raise ValueError('mcp_destination_not_allowed')
        addresses = await asyncio.get_running_loop().getaddrinfo(
            self.server['host'], 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(value[4][0]) for value in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or ip.is_unspecified for ip in ips):
            raise ValueError('mcp_address_not_public')
        address = next((ip for ip in ips if ip.version == 4), ips[0])
        request.url = request.url.copy_with(host=str(address))
        request.headers['Host'] = self.server['host']
        request.headers['Accept-Encoding'] = 'identity'
        request.extensions['sni_hostname'] = self.server['host']
        response = await self.transport.handle_async_request(request)
        if response.is_redirect or response.headers.get('content-encoding', 'identity') != 'identity':
            await response.aclose()
            raise ValueError('mcp_redirect_or_encoding_not_allowed')
        response.stream = LimitedStream(response.stream, self.maximum)
        return response

    async def aclose(self):
        await self.transport.aclose()


async def invoke(server, tool, arguments, settings):
    # Only the worker imports the pinned MCP SDK. Each call has its own session.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def perform():
        async with httpx.AsyncClient(transport=PublicTransport(server), trust_env=False,
                                    follow_redirects=False, timeout=15) as client:
            async with streamable_http_client(server['url'], http_client=client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=settings.agent_mcp_seconds)) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments)
                    if result.isError:
                        return {'error': 'mcp_tool_failed', 'message': '外部工具未完成查询，可修改问题后重试。'}
                    text = '\n'.join(item.text for item in result.content if item.type == 'text')
                    # Ignore remote resource links/images; never follow or execute returned content.
                    return {'source': server['name'], 'untrusted_external_data': True,
                            'text': text[:12000], 'truncated': len(text) > 12000}
    return await asyncio.wait_for(perform(), timeout=settings.agent_mcp_seconds)


def execute(context, server, tool, arguments):
    from .agent_tasks import unseal
    arguments = validate_arguments(server, tool, arguments)

    def check():
        with context.sessions() as db:
            task, _ = context.current(db)
            if (server not in task.config.get('mcp_servers', [])
                    or task.config.get('mcp_versions', {}).get(server) != CATALOG[server]['version']
                    or server not in enabled_servers(db, context.settings)):
                raise ValueError('mcp_connection_disabled')
            events = unseal(task.encrypted_events, context.settings)
            if sum(e['kind'] == 'mcp_started' for e in events) >= context.settings.agent_mcp_calls:
                raise ValueError('mcp_call_limit')
    try:
        check()
    except ValueError:
        return json.dumps({'error': 'mcp_connection_unavailable'}, ensure_ascii=False)
    context.authorize('mcp_read', {'server': server, 'destination': CATALOG[server]['host'],
                                  'tool': tool, 'query': json.dumps(arguments, ensure_ascii=False)})
    # Revocation/cancellation while waiting for approval takes effect before dispatch.
    try:
        check()
    except ValueError:
        return json.dumps({'error': 'mcp_connection_unavailable'}, ensure_ascii=False)
    context.event('mcp_started', text='正在查询 ' + CATALOG[server]['name'], operation='mcp_read')
    async def guarded():
        pending = asyncio.create_task(invoke(CATALOG[server], tool, arguments, context.settings))
        try:
            while not pending.done():
                await asyncio.wait([pending], timeout=1)
                with context.sessions() as db:
                    context.current(db)
                    if server not in enabled_servers(db, context.settings):
                        raise ValueError('mcp_connection_disabled')
            return await pending
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    try:
        result = asyncio.run(guarded())
    except Exception:
        # No SDK/provider exception strings, URLs, headers or remote bodies in user diagnostics.
        result = {'error': 'mcp_unavailable', 'message': '外部文档服务暂时不可用，本次查询未自动重试。'}
    context.event('mcp_result', text='文档查询未完成' if 'error' in result else '文档查询已完成',
                  output=json.dumps(result, ensure_ascii=False))
    return json.dumps(result, ensure_ascii=False)
