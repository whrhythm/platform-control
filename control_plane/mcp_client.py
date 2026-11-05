"""Only trusted services can send reviewed requests to the fixed internal gateway."""
import ssl
import httpx


async def gateway(settings, action, config, *, secret='', addresses=None, tool=None, arguments=None):
    if not settings.mcp_gateway_url or len(settings.mcp_gateway_token) < 32:
        raise RuntimeError('mcp_gateway_unavailable')
    tls = ssl.create_default_context(cafile=settings.workspace_service_ca)
    async with httpx.AsyncClient(verify=tls, trust_env=False, follow_redirects=False,
                                timeout=settings.agent_mcp_seconds + 5) as client:
        response = await client.post(settings.mcp_gateway_url.rstrip('/') + '/exchange',
            headers={'Authorization': 'Bearer ' + settings.mcp_gateway_token},
            json={'action': action, 'url': config['url'], 'secret': secret, 'addresses': addresses or [],
                  'tool': tool or {}, 'arguments': arguments or {}, 'seconds': settings.agent_mcp_seconds})
        if not response.is_success:
            return {'error': 'mcp_unavailable', 'dispatched': False} if response.status_code in (413, 422, 429) else {'error': 'mcp_unavailable'}
        return response.json()
