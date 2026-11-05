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
    def __init__(self, server, maximum=262144, secret=''):
        self.server, self.maximum, self.secret = server, maximum, secret
        # The worker's SSL_CERT_FILE trusts the cluster CA only. Public MCP must
        # use the system/public CA bundle rather than that internal-service override.
        self.transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    async def handle_async_request(self, request):
        if (str(request.url) != self.server['url'] or request.method not in ('POST', 'GET', 'DELETE')
                or request.headers.get('authorization') or request.headers.get('cookie')):
            raise ValueError('mcp_destination_not_allowed')
        addresses = await asyncio.get_running_loop().getaddrinfo(
            self.server['host'], 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(value[4][0]) for value in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or ip.is_unspecified for ip in ips):
            raise ValueError('mcp_address_not_public')
        if self.server.get('addresses') and not {str(ip) for ip in ips if ip.version == 4} <= set(self.server['addresses']):
            raise ValueError('mcp_address_changed_review_required')
        address = next((ip for ip in ips if ip.version == 4), ips[0])
        if self.server.get('addresses') and str(address) not in self.server['addresses']:
            raise ValueError('mcp_address_changed_review_required')
        request.url = request.url.copy_with(host=str(address))
        request.headers['Host'] = self.server['host']
        request.headers['Accept-Encoding'] = 'identity'
        request.extensions['sni_hostname'] = self.server['host']
        if self.secret:
            request.headers['Authorization'] = 'Bearer ' + self.secret
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


def task_connection(context, db, identifier):
    from .mcp_catalog import server, published, secret_for
    task, env = context.current(db)
    row = server(db, identifier)
    if (identifier not in task.config.get('mcp_servers', []) or not published(db, context.settings, row)
            or task.config.get('mcp_versions', {}).get(identifier) != row.revision):
        raise ValueError('mcp_connection_disabled')
    secret, revision = secret_for(db, context.settings, row, env.id)
    if revision != task.config.get('mcp_credentials', {}).get(identifier, 0):
        raise ValueError('mcp_credential_changed')
    return task, row, secret


def describe(context, identifier=''):
    with context.sessions() as db:
        task, _ = context.current(db)
        result = []
        for key in task.config.get('mcp_servers', []):
            if identifier and key != identifier:
                continue
            try:
                _, row, _ = task_connection(context, db, key)
            except (ValueError, HTTPException):
                continue
            result.append({'id': row.id, 'name': row.config['name'], 'tools': [
                {k: tool.get(k) for k in ('name', 'description', 'inputSchema', 'permission')} for tool in row.approved
                if task.config['strategy'] == 'implement' or tool['permission'] == 'read']})
        return json.dumps(result, ensure_ascii=False)


def execute(context, server, tool, arguments):
    from .agent_tasks import unseal
    from .mcp_client import gateway
    from .mcp_catalog import safe_schema
    from .agent_execution import TaskStopped
    from jsonschema import Draft202012Validator
    from referencing import Registry

    def check(budget=True):
        with context.sessions() as db:
            task, row, secret = task_connection(context, db, server)
            approved = next((item for item in row.approved if item['name'] == tool), None)
            if not approved or (task.config['strategy'] != 'implement' and approved['permission'] != 'read'):
                raise ValueError('mcp_tool_not_allowed')
            if not safe_schema(approved['inputSchema']) or len(json.dumps(arguments)) > 8192:
                raise ValueError('mcp_arguments_invalid')
            Draft202012Validator(approved['inputSchema'], registry=Registry()).validate(arguments)
            if budget and sum(e['kind'] == 'mcp_started' for e in unseal(task.encrypted_events, context.settings)) >= context.settings.agent_mcp_calls:
                raise ValueError('mcp_call_limit')
            return row, approved, secret
    try:
        row, approved, _ = check()
    except Exception as error:
        if isinstance(error, TaskStopped):
            raise
        return json.dumps({'error': 'mcp_connection_or_arguments_unavailable'})
    operation = 'mcp_write' if approved['permission'] == 'write' else 'mcp_read'
    context.authorize(operation, {'server': row.config['name'], 'destination': row.config['url'],
                                 'tool': tool, 'query': json.dumps(arguments, ensure_ascii=False),
                                 'external_write': operation == 'mcp_write'})
    try:
        row, approved, secret = check()
    except (ValueError, HTTPException):
        return json.dumps({'error': 'mcp_connection_unavailable'})
    context.event('mcp_started', text='正在调用 ' + row.config['name'], operation=operation)
    async def guarded():
        pending = asyncio.create_task(gateway(context.settings, 'call', row.config, secret=secret,
            addresses=row.addresses, tool=approved, arguments=arguments))
        try:
            while not pending.done():
                await asyncio.wait([pending], timeout=1)
                check(budget=False)
            return await pending
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    try:
        result = asyncio.run(guarded())
    except TaskStopped:
        raise
    except Exception:
        result = {'error': 'mcp_unavailable'}
    context.event('mcp_result', text='外部工具未完成' if 'error' in result else '外部工具已完成',
                  output=json.dumps(result, ensure_ascii=False))
    if operation == 'mcp_write' and 'error' in result and result.get('dispatched') is not False:
        context.stopped, context.stop_reason = True, 'agent_mcp_write_requires_review'
        raise TaskStopped(context.stop_reason)
    return json.dumps(result, ensure_ascii=False)
