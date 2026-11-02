"""Non-executable storage and isolation requirements for an individual user.

No Agent Canvas workload is emitted. Actual sandbox creation, routing, agent
server integration and cleanup require independently verified adapters.
"""
import hashlib
import json


def identity_digest(issuer: str, subject: str) -> str:
    identity = json.dumps([issuer, subject], ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(identity).hexdigest()


def plan_user_environment(issuer: str, subject: str, *, storage_gib: int = 2) -> dict:
    if not isinstance(issuer, str) or not issuer or not isinstance(subject, str) or not subject:
        raise ValueError('A verified issuer and subject are required')
    if isinstance(storage_gib, bool) or not isinstance(storage_gib, int) or not 1 <= storage_gib <= 40:
        raise ValueError('Storage must be between 1 and 40 GiB')
    digest = identity_digest(issuer, subject)
    ns = 'codechat-user-' + digest[:40]
    owner = {'codechat.io/user-hash': digest[:40], 'app.kubernetes.io/managed-by': 'platform-control'}
    metadata = lambda name: {'name': name, 'namespace': ns, 'labels': owner}
    namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns, 'labels': {**owner, 'pod-security.kubernetes.io/enforce': 'restricted'}, 'annotations': {'codechat.io/user-hash-sha256': digest}}}
    quota = {'apiVersion': 'v1', 'kind': 'ResourceQuota', 'metadata': metadata('user-capacity'), 'spec': {'hard': {'requests.storage': f'{storage_gib}Gi', 'persistentvolumeclaims': '1', 'services.nodeports': '0', 'services.loadbalancers': '0'}}}
    pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': metadata('workspace-data'), 'spec': {'storageClassName': 'codechat-workspace-retain', 'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': f'{storage_gib}Gi'}}}}
    isolation = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': metadata('default-deny'), 'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}}
    return {'mode': 'plan', 'executable': False, 'identity_hash': digest, 'namespace': ns, 'storage_gib': storage_gib,
            'runtime_requirement': {'runtime_class': 'kata-qemu-runtime-rs', 'dedicated_sandbox': True, 'workspace_pvc': 'workspace-data', 'shared_orchestration': True},
            'objects': [namespace, quota, pvc, isolation], 'blockers': [
                'No production apply or deprovision adapter or observed readiness exists; the user-level record is metadata only.',
                'A temporary Agent Server and SDK compatibility probe passed; production integration and persistent user volume lifecycle remain unverified.',
                'No authenticated access proxy, model credential relay, or reviewed network egress policy exists.',
                'Project filesystem boundaries, retention, disk accounting and recovery remain unverified.',
                'A live retained-volume probe passed; retained volumes are not backups and require controlled recovery and deletion workflows.',
            ]}
