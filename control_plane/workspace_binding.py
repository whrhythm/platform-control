"""Verify previously recorded workspace volume identities against observations.

This is read-only and is not a Kubernetes client or provisioner. Namespace and
PVC identifiers must originate from trusted create responses; PV identifiers
must come from trusted observations verified against that PVC. Never accept
identifiers from a browser or adopt objects solely by name or labels.
"""
from dataclasses import dataclass
from typing import Any

from .user_environments import identity_digest


@dataclass(frozen=True)
class VolumeBinding:
    namespace_name: str
    namespace_uid: str
    pvc_uid: str
    pv_name: str
    pv_uid: str
    volume_handle: str

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in vars(self).values()):
            raise ValueError('All recorded identifiers must be nonempty strings')


class BindingMismatch(ValueError):
    """Observed resources do not match the trusted binding; never adopt them."""


def verify_volume_binding(issuer: str, subject: str, binding: VolumeBinding,
                          namespace: dict[str, Any], pvc: dict[str, Any], pv: dict[str, Any]) -> str:
    """Return storage-only ready/pending; raise for any unsafe identity mismatch.

    A ready result does not mean a sandbox, backup, file service, quota, or
    complete user environment is ready. Kubernetes reads are not an atomic
    snapshot; a caller must revalidate before each access or mutation.
    """
    if not issuer or not subject:
        raise ValueError('Verified identity is required')
    digest = identity_digest(issuer, subject)
    expected_namespace = 'codechat-user-' + digest[:40]

    def require(condition: bool, description: str):
        if not condition:
            raise BindingMismatch(description)

    nsmeta = namespace.get('metadata', {})
    cm = pvc.get('metadata', {})
    pm = pv.get('metadata', {})
    cs = pvc.get('spec', {})
    ps = pv.get('spec', {})
    ref = ps.get('claimRef', {}) or {}
    csi = ps.get('csi', {}) or {}
    require(binding.namespace_name == expected_namespace, 'Recorded namespace is not owned by this identity')
    require(nsmeta.get('name') == expected_namespace and nsmeta.get('uid') == binding.namespace_uid, 'Namespace identity changed')
    require(nsmeta.get('annotations', {}).get('codechat.io/user-hash-sha256') == digest, 'Namespace identity annotation changed')
    require(nsmeta.get('labels', {}).get('codechat.io/user-hash') == digest[:40], 'Namespace owner label changed')
    require(cm.get('namespace') == expected_namespace and cm.get('name') == 'workspace-data' and cm.get('uid') == binding.pvc_uid, 'PVC identity changed')
    require(cm.get('labels', {}).get('codechat.io/user-hash') == digest[:40], 'PVC owner label changed')
    require(cs.get('storageClassName') == 'codechat-workspace-retain', 'Unexpected PVC storage class')
    require(cs.get('volumeName') == binding.pv_name, 'PVC volume changed')
    require(pm.get('name') == binding.pv_name and pm.get('uid') == binding.pv_uid, 'PV identity changed')
    require(ps.get('storageClassName') == 'codechat-workspace-retain', 'Unexpected PV storage class')
    require(ps.get('persistentVolumeReclaimPolicy') == 'Retain', 'PV is not protected by Retain')
    require(ref.get('namespace') == expected_namespace and ref.get('name') == 'workspace-data' and ref.get('uid') == binding.pvc_uid, 'PV claim reference changed')
    require(csi.get('driver') == 'driver.longhorn.io' and csi.get('volumeHandle') == binding.volume_handle, 'Backing volume identity changed')
    if any(meta.get('deletionTimestamp') for meta in (nsmeta, cm, pm)):
        return 'pending'
    if (namespace.get('status', {}).get('phase') != 'Active' or
            pvc.get('status', {}).get('phase') != 'Bound' or
            pv.get('status', {}).get('phase') != 'Bound'):
        return 'pending'
    return 'ready'
