from copy import deepcopy
from dataclasses import replace

import pytest

from control_plane.user_environments import identity_digest
from control_plane.workspace_binding import BindingMismatch, VolumeBinding, verify_volume_binding


@pytest.fixture
def resources():
    digest = identity_digest('issuer', 'alice')
    ns = 'codechat-user-' + digest[:40]
    binding = VolumeBinding(ns, 'ns-uid', 'claim-uid', 'pv-name', 'pv-uid', 'volume-id')
    namespace = {'metadata': {'name': ns, 'uid': 'ns-uid', 'labels': {'codechat.io/user-hash': digest[:40]}, 'annotations': {'codechat.io/user-hash-sha256': digest}}, 'status': {'phase': 'Active'}}
    pvc = {'metadata': {'name': 'workspace-data', 'namespace': ns, 'uid': 'claim-uid', 'labels': {'codechat.io/user-hash': digest[:40]}}, 'spec': {'storageClassName': 'codechat-workspace-retain', 'volumeName': 'pv-name'}, 'status': {'phase': 'Bound'}}
    pv = {'metadata': {'name': 'pv-name', 'uid': 'pv-uid'}, 'spec': {'storageClassName': 'codechat-workspace-retain', 'persistentVolumeReclaimPolicy': 'Retain', 'claimRef': {'name': 'workspace-data', 'namespace': ns, 'uid': 'claim-uid'}, 'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'volume-id'}}, 'status': {'phase': 'Bound'}}
    return binding, namespace, pvc, pv


def verify(resources, subject='alice'):
    return verify_volume_binding('issuer', subject, *resources)


def test_only_exact_recorded_owner_and_resources_can_be_ready(resources):
    assert verify(resources) == 'ready'
    with pytest.raises(BindingMismatch):
        verify(resources, subject='bob')
    binding, *objects = resources
    with pytest.raises(BindingMismatch):
        verify((replace(binding, pvc_uid='other-uid'), *objects))


@pytest.mark.parametrize(('index', 'path', 'value'), [
    (1, ('metadata', 'uid'), 'replacement'),
    (1, ('metadata', 'annotations', 'codechat.io/user-hash-sha256'), 'forged'),
    (2, ('metadata', 'uid'), 'replacement'),
    (2, ('metadata', 'namespace'), 'other'),
    (2, ('spec', 'volumeName'), 'other-pv'),
    (2, ('spec', 'storageClassName'), 'longhorn'),
    (3, ('metadata', 'uid'), 'replacement'),
    (3, ('spec', 'claimRef', 'uid'), 'other-claim'),
    (3, ('spec', 'persistentVolumeReclaimPolicy'), 'Delete'),
    (3, ('spec', 'csi', 'driver'), 'other.csi'),
    (3, ('spec', 'csi', 'volumeHandle'), 'other-volume'),
])
def test_mutated_observation_is_never_adopted(resources, index, path, value):
    changed = list(deepcopy(resources))
    node = changed[index]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(BindingMismatch):
        verify(changed)


@pytest.mark.parametrize(('index', 'path', 'value'), [
    (1, ('status', 'phase'), 'Terminating'),
    (2, ('status', 'phase'), 'Pending'),
    (3, ('status', 'phase'), 'Released'),
    (2, ('metadata', 'deletionTimestamp'), '2026-01-01T00:00:00Z'),
])
def test_not_ready_when_resources_are_deleting_or_not_bound(resources, index, path, value):
    changed = list(deepcopy(resources))
    node = changed[index]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert verify(changed) == 'pending'


def test_missing_recorded_identifiers_are_rejected():
    with pytest.raises(ValueError):
        VolumeBinding('ns', 'uid', '', 'pv', 'pvuid', 'volume')
