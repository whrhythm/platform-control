"""Check allowed and forbidden browser origins against the live HTTPS platform."""
import http.client
from pathlib import Path
import socket
import ssl

real_lookup = socket.getaddrinfo


def lookup(host, port, *args, **kwargs):
    return real_lookup('192.168.10.30' if host == 'platform.k3s.local' else host, port, *args, **kwargs)


socket.getaddrinfo = lookup
ca = Path(__file__).resolve().parents[1] / 'release/internal-ca.crt'
for origin, expected in [('https://chat.k3s.local:30443', 200), ('https://untrusted.example', 400)]:
    connection = http.client.HTTPSConnection('platform.k3s.local', 30443, timeout=10, context=ssl.create_default_context(cafile=str(ca)))
    connection.request('OPTIONS', '/v1/me/personal-tenant', headers={'Origin': origin, 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type'})
    response = connection.getresponse()
    response.read()
    if response.status != expected:
        raise RuntimeError(f'{origin}: expected {expected}, got {response.status}')
    allow = response.getheader('Access-Control-Allow-Origin')
    if (expected == 200 and allow != origin) or (expected != 200 and allow):
        raise RuntimeError(f'{origin}: unexpected allowed origin')
    print(f'{origin}: {response.status}, expected CORS policy')
    connection.close()
