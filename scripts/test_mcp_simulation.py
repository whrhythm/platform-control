"""Run against Postman local simulation; production has no destination override."""
import asyncio
import os
from types import SimpleNamespace

import httpx

from control_plane import agent_mcp


async def main():
    base = os.environ['MCP_SIMULATION_URL'].rstrip('/')
    # Only this test harness bypasses public DNS restrictions for a loopback mock.
    original = agent_mcp.PublicTransport
    class LocalTransport(httpx.AsyncBaseTransport):
        def __init__(self, *_args):
            self.inner = httpx.AsyncHTTPTransport(retries=0)
        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            response.stream = agent_mcp.LimitedStream(response.stream, 262144)
            return response
        async def aclose(self):
            await self.inner.aclose()
    agent_mcp.PublicTransport = LocalTransport
    try:
        async with httpx.AsyncClient(trust_env=False) as admin:
            for mode in ['success', 'tool_error', 'unavailable', 'slow', 'large']:
                response = await admin.post(base + '/__seed', json={'mode': mode})
                response.raise_for_status()
                failed = False
                result = None
                try:
                    result = await agent_mcp.invoke({**agent_mcp.CATALOG['deepwiki'], 'url': base + '/mcp'},
                        'read_wiki_structure', {'repoName':'example/project'}, SimpleNamespace(agent_mcp_seconds=3))
                except Exception:
                    failed = True
                assert failed == (mode in ('unavailable', 'slow', 'large')), mode
                if not failed:
                    assert ('error' in result) == (mode == 'tool_error')
                    assert 'private diagnostic' not in str(result)
                assert (await admin.get(base + '/__calls')).json()['calls'] == 1
                print(mode + ': verified; one call, no automatic replay', flush=True)
    finally:
        agent_mcp.PublicTransport = original


if __name__ == '__main__':
    asyncio.run(main())
