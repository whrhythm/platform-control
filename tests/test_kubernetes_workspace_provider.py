from copy import deepcopy

import httpx
import pytest

from control_plane.kubernetes_workspace_provider import KubernetesProviderError, KubernetesWorkspaceProvider


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


@pytest.mark.parametrize(('uid', 'phase', 'volume', 'expected_calls'), [
    ('other-uid', 'Bound', 'pv-one', 1),
    ('pvc-uid', 'Bound', '../other', 1),
    ('pvc-uid', 'Pending', None, 1),
    ('pvc-uid', 'Bound', 'pv-one', 2),
])
def test_claim_identity_is_checked_before_pv_read(uid, phase, volume, expected_calls):
    provider = make_provider()
    pvc = deepcopy(provider.objects['PersistentVolumeClaim'])
    pvc['metadata']['uid'] = uid
    pvc['status'] = {'phase': phase}
    if volume:
        pvc['spec']['volumeName'] = volume
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=pvc if len(calls) == 1 else {'metadata': {'name': 'pv-one'}})

    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handler), base_url='https://kubernetes.example.test/')
    try:
        if uid != 'pvc-uid' or volume == '../other':
            with pytest.raises(KubernetesProviderError):
                provider.inspect_claim_volume('pvc-uid')
        else:
            claim, pv = provider.inspect_claim_volume('pvc-uid')
            assert claim['metadata']['uid'] == 'pvc-uid'
            assert (pv is None) == (phase == 'Pending')
        assert len(calls) == expected_calls
    finally:
        provider.close()
