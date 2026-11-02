import pytest
from sqlalchemy import select

from control_plane.models import UserEnvironment, WorkspaceProvisioningIntent
from control_plane.settings import get_settings
from control_plane.workspace_runtime import file_service_objects


def test_workspace_bridge_requires_service_identity_and_reuses_space(harness, monkeypatch):
    client, _token = harness
    monkeypatch.setenv('PLATFORM_WORKSPACE_FILES_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_LIBRECHAT_SERVICE_TOKEN', 's' * 40)
    get_settings.cache_clear()
    path = '/internal/v1/workspace/ensure'
    assert client.post(path).status_code == 403
    assert client.post(path, headers={'X-CodeChat-Subject': 'alice'}).status_code == 403
    headers = {'X-CodeChat-Service-Token': 's' * 40, 'X-CodeChat-Subject': 'alice'}
    assert client.post(path, headers=headers).json() == {'status': 'preparing', 'storage_gib': 2}
    assert client.post(path, headers=headers).status_code == 200
    assert client.post(path, headers={**headers, 'X-CodeChat-Subject': 'bob'}).status_code == 200
    with client.session_factory() as db:
        environments = db.scalars(select(UserEnvironment)).all()
        assert len(environments) == 2
        assert len({env.identity_hash for env in environments}) == 2
        assert all(db.get(WorkspaceProvisioningIntent, env.id).state == 'requested' for env in environments)
    assert client.get('/internal/v1/workspace/directories', headers=headers).status_code == 409
    assert client.post(path, headers=headers).headers['cache-control'] == 'no-store'


def test_file_service_manifest_is_isolated_and_uses_existing_claim():
    digest = 'a' * 64
    objects = file_service_objects('codechat-user-' + digest[:40], digest,
                                   'harbor.k3s.local/internal/platform@sha256:' + 'b' * 64, 't' * 48)
    deployment = next(obj for obj in objects if obj['kind'] == 'Deployment')
    pod = deployment['spec']['template']['spec']
    assert pod['runtimeClassName'] == 'kata-qemu-runtime-rs'
    assert pod['automountServiceAccountToken'] is False
    assert pod['volumes'][0]['persistentVolumeClaim']['claimName'] == 'workspace-data'
    assert pod['containers'][0]['securityContext']['readOnlyRootFilesystem'] is True
    assert not any(obj['kind'] == 'PersistentVolumeClaim' for obj in objects)
    with pytest.raises(ValueError):
        file_service_objects('other', digest, 'image:latest', 't' * 48)
