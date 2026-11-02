import pytest

from control_plane.sandbox_manifest import render_agent_sandbox
from control_plane.user_environments import identity_digest


IMAGE = 'harbor.k3s.local/internal/agent-server@sha256:' + 'a' * 64


def sandbox(subject):
    identity = identity_digest('https://issuer/', subject)
    return render_agent_sandbox(namespace='codechat-user-' + identity[:40], identity_hash=identity, image=IMAGE)


def test_sandbox_uses_distinct_user_namespace_kata_and_user_pvc():
    a, b = sandbox('alice'), sandbox('bob')
    assert a['metadata']['namespace'] != b['metadata']['namespace']
    assert a['metadata']['annotations']['codechat.io/user-hash-sha256'] != b['metadata']['annotations']['codechat.io/user-hash-sha256']
    pod = a['spec']['podTemplate']['spec']
    assert pod['runtimeClassName'] == 'kata-qemu-runtime-rs'
    assert pod['automountServiceAccountToken'] is False
    assert pod['securityContext']['runAsNonRoot'] is True
    assert pod['volumes'][0]['persistentVolumeClaim']['claimName'] == 'workspace-data'
    container = pod['containers'][0]
    assert container['command'][:3] == ['python', '-m', 'openhands.agent_server']
    assert container['securityContext']['allowPrivilegeEscalation'] is False
    assert container['securityContext']['capabilities']['drop'] == ['ALL']
    assert {item['subPath'] for item in container['volumeMounts']} == {'workspace', 'openhands'}
    assert 'hostPath' not in str(a) and 'LITELLM_MASTER_KEY' not in str(a)


def test_cross_user_namespace_or_mutable_image_are_rejected():
    digest = identity_digest('issuer', 'alice')
    with pytest.raises(ValueError, match='Namespace'):
        render_agent_sandbox(namespace='codechat-user-' + 'b' * 40, identity_hash=digest, image=IMAGE)
    with pytest.raises(ValueError, match='immutable'):
        render_agent_sandbox(namespace='codechat-user-' + digest[:40], identity_hash=digest, image='example/agent:latest')
