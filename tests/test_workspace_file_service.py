import errno
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from control_plane.workspace_file_service import create_app, server_options
from control_plane.workspace_files import WorkspaceFiles


TOKEN_A = 'a' * 40
TOKEN_B = 'b' * 40


def headers(token):
    return {'Authorization': 'Bearer ' + token}


@pytest.fixture
def clients(tmp_path):
    a = tmp_path / 'user-a'
    b = tmp_path / 'user-b'
    a.mkdir()
    b.mkdir()
    with TestClient(create_app(str(a), TOKEN_A, max_file_bytes=32)) as client_a:
        with TestClient(create_app(str(b), TOKEN_B, max_file_bytes=32)) as client_b:
            yield client_a, client_b, a, b


def test_service_auth_is_required_and_never_accepts_another_users_token(clients):
    a, b, _, _ = clients
    assert a.get('/healthz').status_code == 200
    assert a.get('/v1/directories').status_code == 401
    assert a.get('/v1/directories', headers=headers(TOKEN_B)).status_code == 401
    assert b.get('/v1/directories', headers=headers(TOKEN_A)).status_code == 401
    response = a.get('/v1/directories', headers=headers(TOKEN_A))
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert a.get('/openapi.json').status_code == 404


def test_files_are_isolated_and_overwrites_are_explicit(clients):
    a, b, _, _ = clients
    assert a.post('/v1/directories', params={'path': 'project'}, headers=headers(TOKEN_A)).status_code == 201
    url = '/v1/file'
    assert a.put(url, params={'path': 'project/main.py'}, headers=headers(TOKEN_A), content=b'print(1)').status_code == 201
    assert a.get(url, params={'path': 'project/main.py'}, headers=headers(TOKEN_A)).content == b'print(1)'
    assert b.get(url, params={'path': 'project/main.py'}, headers=headers(TOKEN_B)).status_code == 404
    assert a.put(url, params={'path': 'project/main.py'}, headers=headers(TOKEN_A), content=b'changed').status_code == 409
    assert a.get(url, params={'path': 'project/main.py'}, headers=headers(TOKEN_A)).content == b'print(1)'
    assert a.put(url, params={'path': 'project/main.py', 'overwrite': 'true'}, headers=headers(TOKEN_A), content=b'changed').status_code == 201
    assert a.get(url, params={'path': 'project/main.py'}, headers=headers(TOKEN_A)).content == b'changed'


def test_invalid_path_links_and_oversize_do_not_leak_or_modify_data(clients, tmp_path):
    a, _, root, _ = clients
    outside = tmp_path / 'secret'
    outside.write_bytes(b'do-not-read')
    (root / 'link').symlink_to(outside)
    os.link(outside, root / 'hardlink')
    for path in ('../secret', '/etc/passwd', 'link', 'hardlink'):
        response = a.get('/v1/file', params={'path': path}, headers=headers(TOKEN_A))
        assert response.status_code == 400
        assert b'do-not-read' not in response.content
        assert str(tmp_path).encode() not in response.content
    assert a.put('/v1/file', params={'path': 'good'}, headers=headers(TOKEN_A), content=b'original').status_code == 201
    assert a.put('/v1/file', params={'path': 'good', 'overwrite': 'true'}, headers=headers(TOKEN_A), content=b'x' * 33).status_code == 413
    assert a.get('/v1/file', params={'path': 'good'}, headers=headers(TOKEN_A)).content == b'original'
    assert outside.read_bytes() == b'do-not-read'


def test_disk_full_returns_safe_error(clients, monkeypatch, tmp_path):
    a, _, _, _ = clients
    def disk_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, 'unsafe host filesystem detail')
    monkeypatch.setattr(WorkspaceFiles, 'write_file', disk_full)
    response = a.put('/v1/file', params={'path': 'file'}, headers=headers(TOKEN_A), content=b'data')
    assert response.status_code == 507
    assert b'unsafe host' not in response.content
    assert str(tmp_path).encode() not in response.content


def test_startup_requires_existing_volume_and_strong_length_token(tmp_path):
    with pytest.raises(ValueError):
        create_app(str(tmp_path), 'short')
    with pytest.raises(FileNotFoundError):
        create_app(str(tmp_path / 'missing'), TOKEN_A)


def test_contract_describes_auth_and_binary_transfer(tmp_path):
    schema = create_app(str(tmp_path), TOKEN_A).openapi()
    assert schema['paths']['/v1/file']['put']['security'] == [{'WorkspaceServiceToken': []}]
    body = schema['paths']['/v1/file']['put']['requestBody']['content']
    assert body['application/octet-stream']['schema']['format'] == 'binary'
    assert 'application/octet-stream' in schema['paths']['/v1/file']['get']['responses']['200']['content']


def test_public_listener_requires_tls_and_logs_remain_off(tmp_path):
    assert server_options({}) == {'host': '127.0.0.1', 'port': 8090, 'access_log': False}
    with pytest.raises(ValueError, match='requires TLS'):
        server_options({'WORKSPACE_SERVICE_HOST': '0.0.0.0'})
    with pytest.raises(ValueError, match='Both TLS'):
        server_options({'WORKSPACE_TLS_CERT': 'cert.pem'})
    with pytest.raises(ValueError, match='unavailable'):
        server_options({'WORKSPACE_TLS_CERT': 'missing.pem', 'WORKSPACE_TLS_KEY': 'missing-key.pem'})
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert.write_text('test-placeholder')
    key.write_text('test-placeholder')
    options = server_options({'WORKSPACE_SERVICE_HOST': '0.0.0.0', 'WORKSPACE_TLS_CERT': str(cert), 'WORKSPACE_TLS_KEY': str(key)})
    assert options['ssl_certfile'] == str(cert)
    assert options['ssl_keyfile'] == str(key)
    assert options['access_log'] is False


def test_real_tls_requires_trusted_certificate_and_service_token(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'workspace-test')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(hours=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'test-ca.pem', tmp_path / 'test-key.pem'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    env = {**os.environ, 'WORKSPACE_ROOT': str(tmp_path), 'WORKSPACE_SERVICE_TOKEN': TOKEN_A,
           'WORKSPACE_SERVICE_PORT': str(port), 'WORKSPACE_TLS_CERT': str(cert_path), 'WORKSPACE_TLS_KEY': str(key_path)}
    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen([sys.executable, '-m', 'control_plane.workspace_file_service'], cwd=root, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f'https://127.0.0.1:{port}'
    try:
        deadline = time.monotonic() + 20
        with httpx.Client(verify=ssl.create_default_context(cafile=str(cert_path)), trust_env=False, timeout=1) as client:
            while True:
                try:
                    response = client.get(base + '/healthz')
                    break
                except httpx.ConnectError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError('TLS test service failed to start') from None
                    time.sleep(0.1)
            assert response.status_code == 200
            assert client.get(base + '/v1/directories').status_code == 401
            assert client.get(base + '/v1/directories', headers=headers(TOKEN_A)).status_code == 200
        with httpx.Client(verify=True, trust_env=False, timeout=2) as client:
            with pytest.raises(httpx.ConnectError):
                client.get(base + '/healthz')
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
