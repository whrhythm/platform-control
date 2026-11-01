import pytest
from control_plane.user_environments import plan_user_environment


def test_each_user_gets_deterministic_separate_namespace_and_volume_plan():
    alice = plan_user_environment('https://issuer/', 'alice')
    assert alice == plan_user_environment('https://issuer/', 'alice')
    bob = plan_user_environment('https://issuer/', 'bob')
    assert alice['namespace'] != bob['namespace']
    assert alice['executable'] is False
    pvc = next(obj for obj in alice['objects'] if obj['kind'] == 'PersistentVolumeClaim')
    assert pvc['metadata']['namespace'] == alice['namespace']
    assert pvc['spec']['resources']['requests']['storage'] == '2Gi'
    assert alice['runtime_requirement']['runtime_class'] == 'kata-qemu-runtime-rs'
    assert alice['runtime_requirement']['dedicated_sandbox'] is True
    assert alice['runtime_requirement']['shared_orchestration'] is True
    assert not any(obj['kind'] in ('Deployment', 'StatefulSet', 'Pod', 'Sandbox') for obj in alice['objects'])
    assert not any('hostPath' in str(obj) or obj['kind'] == 'Secret' for obj in alice['objects'])
    assert all('alice' not in str(obj) for obj in alice['objects'])


@pytest.mark.parametrize('size', [0, 41, True, '5'])
def test_invalid_storage_is_rejected(size):
    with pytest.raises(ValueError):
        plan_user_environment('issuer', 'subject', storage_gib=size)


def test_ambiguous_identity_is_rejected():
    with pytest.raises(ValueError):
        plan_user_environment('', 'subject')
    assert plan_user_environment('ab', 'c')['namespace'] != plan_user_environment('a', 'bc')['namespace']
