import asyncio
import json
import socket

import httpx
import pytest
from pydantic import ValidationError

from control_plane.agent_execution import TaskTools, TaskStopped
from control_plane.agent_mcp import CATALOG, PublicTransport, LimitedStream, execute, validate_arguments
from control_plane.agent_tasks import unseal
from control_plane.models import AIConfiguration, AgentTask, AuditEvent
from control_plane.settings import get_settings
from test_agent_workbench import workspace, create


def select_mcp(workspace):
    client, headers, base = workspace
    preference = client.get(base + '/preferences', headers=headers).json()
    preference['mcp_servers'] = ['deepwiki']
    assert client.put(base + '/preferences', headers=headers, json=preference).status_code == 200
    task = create(workspace)
    with client.session_factory.begin() as db:
        row = db.get(AgentTask, task.id)
        row.status = 'running'
        row.config = {**row.config, 'strategy': 'review', 'approval': 'workspace_auto'}
    return task, TaskTools(client.session_factory, task.id, None, get_settings(), None)


def test_mcp_admin_permissions_audit_cas_without_provider_dependency(workspace, harness):
    client, _, _ = workspace
    _, token = harness
    admin = {'Authorization': 'Bearer ' + token('operator', groups=['platform-admins'])}
    ordinary = {'Authorization': 'Bearer ' + token()}
    path = '/v1/admin/ai/mcp'
    assert client.get(path, headers=ordinary).status_code == 403
    assert client.put(path, headers=ordinary, json={'revision': 0, 'servers': []}).status_code == 403
    initial = client.get(path, headers=admin).json()
    assert initial['servers'][0]['enabled']
    assert client.put(path, headers=admin, json={'revision': 0, 'servers': ['shell']}).status_code == 422
    assert client.put(path, headers=admin, json={'revision': 0, 'servers': [], 'url': 'http://localhost'}).status_code == 422
    assert client.put(path, headers=admin, json={'revision': 0, 'servers': []}).json()['servers'][0]['enabled'] is False
    assert client.put(path, headers=admin, json={'revision': 0, 'servers': ['deepwiki']}).status_code == 409
    from sqlalchemy import select
    with client.session_factory() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == 'mcp.policy.updated'))


def test_selection_frozen_owner_scoped_and_rejected_when_disabled(workspace):
    client, headers, base = workspace
    task, tools = select_mcp(workspace)
    saved = client.get(base + '/preferences', headers=headers).json()
    saved['mcp_servers'] = []
    assert client.put(base + '/preferences', headers=headers, json=saved).status_code == 200
    with client.session_factory() as db:
        assert db.get(AgentTask, task.id).config['mcp_servers'] == ['deepwiki']
    other = client.get(base + '/preferences', headers={**headers, 'X-CodeChat-Subject': 'bob'}).json()
    assert other['mcp_servers'] == []
    with client.session_factory.begin() as db:
        from control_plane.ai_policy import effective_policy
        policy = effective_policy(db, get_settings())
        db.add(AIConfiguration(id='default', config={**policy, 'mcp_servers': []}))
    assert json.loads(execute(tools, 'deepwiki', 'read_wiki_structure', {'repoName': 'example/project'}))['error'] == 'mcp_connection_or_arguments_unavailable'
    saved = client.get(base + '/preferences', headers=headers).json()
    saved['mcp_servers'] = ['deepwiki']
    assert client.put(base + '/preferences', headers=headers, json=saved).status_code == 409


@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_every_external_query_requires_approval_even_review_auto(workspace, monkeypatch, decision):
    client, headers, base = workspace
    task, tools = select_mcp(workspace)
    calls = []
    async def provider(*args, **kwargs):
        calls.append(args)
        return {'text': 'public documentation', 'untrusted_external_data': True}
    monkeypatch.setattr('control_plane.mcp_client.gateway', provider)
    def approve(_):
        current = client.get(base + '/tasks/' + task.id, headers=headers).json()
        assert not calls
        assert current['status'] == 'awaiting_approval'
        assert current['events'][-1]['arguments']['destination'] == 'https://mcp.deepwiki.com/mcp'
        assert client.post(base + '/tasks/' + task.id + '/decision', headers=headers,
            json={'operation_id': current['pending']['id'], 'decision': decision}).status_code == 200
    monkeypatch.setattr('control_plane.agent_execution.time.sleep', approve)
    if decision == 'reject':
        with pytest.raises(TaskStopped, match='rejected'):
            execute(tools, 'deepwiki', 'read_wiki_structure', {'repoName': 'example/project'})
        assert not calls
    else:
        assert json.loads(execute(tools, 'deepwiki', 'read_wiki_structure', {'repoName': 'example/project'}))['text'] == 'public documentation'
        assert len(calls) == 1
        with client.session_factory() as db:
            assert unseal(db.get(AgentTask, task.id).encrypted_events, get_settings())[-1]['kind'] == 'mcp_result'


def test_unselected_and_invalid_tools_never_call_external_service(workspace):
    client, _, _ = workspace
    task = create(workspace)
    with client.session_factory.begin() as db:
        db.get(AgentTask, task.id).status = 'running'
    tools = TaskTools(client.session_factory, task.id, None, get_settings(), None)
    assert 'mcp_connection_or_arguments_unavailable' in execute(tools, 'deepwiki', 'read_wiki_structure', {'repoName': 'example/project'})
    with pytest.raises(ValueError):
        validate_arguments('deepwiki', 'execute_command', {})
    with pytest.raises(ValidationError):
        validate_arguments('deepwiki', 'read_wiki_structure', {'repoName': 'example/project', 'url': 'http://localhost'})


def test_revocation_while_waiting_for_consent_prevents_dispatch(workspace, monkeypatch):
    client, _, _ = workspace
    task, tools = select_mcp(workspace)
    def revoked_after_approval(*_args):
        from control_plane.ai_policy import effective_policy
        with client.session_factory.begin() as db:
            db.add(AIConfiguration(id='default', config={**effective_policy(db, get_settings()), 'mcp_servers': []}))
        return 'approved'
    monkeypatch.setattr(tools, 'authorize', revoked_after_approval)
    async def never(*_args, **_kwargs):
        pytest.fail('revoked connector dispatched')
    monkeypatch.setattr('control_plane.mcp_client.gateway', never)
    assert 'mcp_connection_unavailable' in execute(tools, 'deepwiki', 'read_wiki_structure', {'repoName':'example/project'})


def test_query_failure_is_safe_and_not_automatically_replayed(workspace, monkeypatch):
    client, _, _ = workspace
    task, tools = select_mcp(workspace)
    monkeypatch.setattr(tools, 'authorize', lambda *_: 'synthetic-approved')
    calls = []
    async def failed(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('private provider diagnostic secret')
    monkeypatch.setattr('control_plane.mcp_client.gateway', failed)
    result = execute(tools, 'deepwiki', 'read_wiki_structure', {'repoName':'example/project'})
    assert len(calls) == 1 and json.loads(result)['error'] == 'mcp_unavailable'
    assert 'secret' not in result
    with client.session_factory() as db:
        events = unseal(db.get(AgentTask, task.id).encrypted_events, get_settings())
        assert events[-1]['kind'] == 'mcp_result' and 'secret' not in str(events)


@pytest.mark.parametrize('address', ['127.0.0.1', '10.43.0.1', '169.254.169.254', '192.168.110.126', '172.16.11.6', '::1', 'fd00::1', '224.0.0.1'])
def test_transport_rejects_private_dns_before_connect(address):
    async def run():
        loop = asyncio.get_running_loop()
        async def resolve(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
        loop.getaddrinfo = resolve
        async with PublicTransport(CATALOG['deepwiki']) as transport:
            with pytest.raises(ValueError, match='not_public'):
                await transport.handle_async_request(httpx.Request('POST', CATALOG['deepwiki']['url']))
    asyncio.run(run())


def test_transport_pins_ip_preserves_verified_tls_and_blocks_redirect():
    async def run():
        loop = asyncio.get_running_loop()
        async def resolve(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        loop.getaddrinfo = resolve
        async def handler(request):
            assert request.url.host == '8.8.8.8'
            assert request.headers['Host'] == 'mcp.deepwiki.com'
            assert request.extensions['sni_hostname'] == 'mcp.deepwiki.com'
            return httpx.Response(302, headers={'Location': 'https://127.0.0.1/'})
        transport = PublicTransport(CATALOG['deepwiki'])
        await transport.transport.aclose()
        transport.transport = httpx.MockTransport(handler)
        async with transport:
            with pytest.raises(ValueError, match='redirect'):
                await transport.handle_async_request(httpx.Request('POST', CATALOG['deepwiki']['url']))
            with pytest.raises(ValueError, match='destination'):
                await transport.handle_async_request(httpx.Request('POST', 'https://evil.example/mcp'))
    asyncio.run(run())


def test_stream_limit():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'a' * 20
            yield b'b' * 20
    async def run():
        with pytest.raises(ValueError, match='too_large'):
            async for _ in LimitedStream(Stream(), 30):
                pass
    asyncio.run(run())


def test_public_transport_ignores_internal_only_ca_environment(monkeypatch):
    monkeypatch.setenv('SSL_CERT_FILE', '/nonexistent/internal-only-ca.pem')
    async def run():
        async with PublicTransport(CATALOG['deepwiki']):
            pass
    asyncio.run(run())
