"""Integration check using the local, synthetic Postman LiteLLM simulation."""
import os
import socket
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from control_plane.models import UserEnvironment, WorkspaceVolumeBinding
from control_plane.settings import get_settings


def test_real_login_http_path_reuses_one_environment(harness, monkeypatch):
    url = os.environ.get('CODECHAT_LITELLM_SIM_URL')
    if not url:
        pytest.skip('requires the local LiteLLM Postman simulation')
    client, token = harness
    monkeypatch.setenv('PLATFORM_LITELLM_URL', url)
    monkeypatch.setenv('PLATFORM_LITELLM_MASTER_KEY', 'synthetic-master')
    monkeypatch.setenv('PLATFORM_LITELLM_MODELS', 'simulation-model')
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('PLATFORM_LIBRECHAT_SERVICE_TOKEN', 's' * 40)
    get_settings.cache_clear()
    subject = 'simulation-' + uuid4().hex
    headers = {'Authorization': 'Bearer ' + token(subject), 'X-CodeChat-Service-Token': 's' * 40}
    first = client.post('/internal/v1/litellm/key', headers=headers)
    second = client.post('/internal/v1/litellm/key', headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()['key'] == second.json()['key']
    with client.session_factory() as db:
        environments = db.scalars(select(UserEnvironment).where(UserEnvironment.subject == subject)).all()
        assert len(environments) == 1 and environments[0].first_project_id is None
        assert db.get(WorkspaceVolumeBinding, environments[0].id) is None


def test_login_provider_unreachable_never_duplicates_plan(harness, monkeypatch):
    client, token = harness
    with socket.socket() as closed_port:
        closed_port.bind(('127.0.0.1', 0))
        monkeypatch.setenv('PLATFORM_LITELLM_URL', f'http://127.0.0.1:{closed_port.getsockname()[1]}')
        monkeypatch.setenv('PLATFORM_LITELLM_MASTER_KEY', 'synthetic-master')
        monkeypatch.setenv('PLATFORM_LITELLM_MODELS', 'simulation-model')
        monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
        monkeypatch.setenv('PLATFORM_LIBRECHAT_SERVICE_TOKEN', 's' * 40)
        get_settings.cache_clear()
        subject = 'unavailable-' + uuid4().hex
        headers = {'Authorization': 'Bearer ' + token(subject), 'X-CodeChat-Service-Token': 's' * 40}
        assert client.post('/internal/v1/litellm/key', headers=headers).status_code == 503
        assert client.post('/internal/v1/litellm/key', headers=headers).status_code == 503
    with client.session_factory() as db:
        environments = db.scalars(select(UserEnvironment).where(UserEnvironment.subject == subject)).all()
        assert len(environments) == 1 and environments[0].status == 'requested'
        assert db.get(WorkspaceVolumeBinding, environments[0].id) is None
