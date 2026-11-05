"""Exercise actual MCP SDK/discovery/validation over Postman HTTP; synthetic data only."""
import asyncio
import os
import socket
import httpx
from control_plane import mcp_gateway
from control_plane.agent_mcp import LimitedStream


async def main():
    base=os.environ['MCP_SIMULATION_URL'].rstrip('/')
    class SimulationTransport(httpx.AsyncBaseTransport):
        def __init__(self, server, secret='', **kwargs):
            self.inner=httpx.AsyncHTTPTransport(retries=0)
            self.secret=secret
        async def handle_async_request(self, request):
            request.url=httpx.URL(base+'/mcp')
            if self.secret:request.headers['Authorization']='Bearer '+self.secret
            response=await self.inner.handle_async_request(request)
            response.stream=LimitedStream(response.stream,262144)
            return response
        async def aclose(self):await self.inner.aclose()
    mcp_gateway.PublicTransport=SimulationTransport
    loop=asyncio.get_running_loop()
    original=loop.getaddrinfo
    async def resolve(host,*args,**kwargs):
        if host in ('docs.example.com',b'docs.example.com'):
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]
        return await original(host,*args,**kwargs)
    loop.getaddrinfo=resolve
    async with httpx.AsyncClient(trust_env=False) as admin:
        await admin.post(base+'/__seed',json={'mode':'success'})
        discovered=await mcp_gateway.exchange(mcp_gateway.Envelope(action='discover',url='https://docs.example.com/mcp'))
        assert discovered['tools'][0]['supported']
        tool=discovered['tools'][0]
        for mode in ['success','tool_error','unavailable','slow','large','drift']:
            await admin.post(base+'/__seed',json={'mode':mode})
            result=None;failed=False
            try:
                result=await asyncio.wait_for(mcp_gateway.exchange(mcp_gateway.Envelope(action='call',url='https://docs.example.com/mcp',
                    addresses=discovered['addresses'],tool=tool,arguments={'repoName':'example/project'},seconds=3)),3)
            except Exception:failed=True
            assert failed==(mode in ('unavailable','slow','large')),mode
            if not failed:
                assert ('error' in result)==(mode in ('tool_error','drift'))
                if mode=='drift':assert result['dispatched'] is False
            assert (await admin.get(base+'/__calls')).json()['calls']==(0 if mode=='drift' else 1)
            print(mode+': gateway verified; no replay',flush=True)


if __name__=='__main__':asyncio.run(main())
