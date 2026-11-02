from copy import deepcopy

import pytest

from control_plane.kubernetes_workspace_provider import KubernetesWorkspaceProvider


def make_provider(url='https://kubernetes.example.test', **overrides):
    options = dict(api_url=url, token='synthetic-token', issuer='issuer', subject='alice', storage_gib=2)
    options.update(overrides)
    return KubernetesWorkspaceProvider(**options)


def test_rejects_insecure_or_user_supplied_urls():
    for url in ('http://example.test', 'https://name:secret@example.test', 'https://example.test/?x=1'):
        with pytest.raises(ValueError):
            make_provider(url)
    with pytest.raises(ValueError):
        make_provider('http://example.test', allow_test_loopback_http=True)
    with pytest.raises(ValueError):
        make_provider('http://127.0.0.1:1234')


def test_rejects_arbitrary_objects_and_other_user_namespace_before_network():
    provider = make_provider()
    try:
        pvc = deepcopy(provider.objects['PersistentVolumeClaim'])
        pvc['metadata']['namespace'] = 'someone-else'
        with pytest.raises(ValueError):
            provider.create_pvc(pvc)
        with pytest.raises(ValueError):
            provider.read_namespace('someone-else')
        provider.objects['PersistentVolumeClaim']['spec']['storageClassName'] = 'longhorn'
        with pytest.raises(ValueError):
            provider.create_pvc(provider.objects['PersistentVolumeClaim'])
        assert not hasattr(provider, 'delete')
    finally:
        provider.close()
