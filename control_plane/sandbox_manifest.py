"""Render a candidate per-user Agent Sandbox; this module cannot apply it.

Only the shared platform worker should eventually call this after validating
the environment's ownership and allocating a unique sandbox session secret.
Routing, model credentials, reconciliation and deletion are deliberately not
implemented here, so a generated manifest is not a runnable user environment.
"""
import re


_DNS_LABEL = re.compile(r'^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$')
_SHA256_IMAGE = re.compile(r'^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$')
_HASH = re.compile(r'^[0-9a-f]{64}$')


def render_agent_sandbox(*, namespace: str, identity_hash: str, image: str) -> dict:
    if not isinstance(identity_hash, str) or not _HASH.fullmatch(identity_hash):
        raise ValueError('A complete verified identity hash is required')
    expected_namespace = 'codechat-user-' + identity_hash[:40]
    if namespace != expected_namespace or not _DNS_LABEL.fullmatch(namespace):
        raise ValueError('Namespace does not match the verified identity')
    if not isinstance(image, str) or not _SHA256_IMAGE.fullmatch(image):
        raise ValueError('Agent Server image must use an immutable sha256 digest')

    labels = {
        'app.kubernetes.io/managed-by': 'platform-control',
        'codechat.io/user-hash': identity_hash[:40],
        'codechat.io/component': 'agent-runtime',
    }
    return {
        'apiVersion': 'agents.x-k8s.io/v1beta1',
        'kind': 'Sandbox',
        'metadata': {'name': 'agent-runtime', 'namespace': namespace, 'labels': labels,
                     'annotations': {'codechat.io/user-hash-sha256': identity_hash}},
        'spec': {
            'service': False,
            'shutdownPolicy': 'Retain',
            'operatingMode': 'Running',
            'podTemplate': {
                'metadata': {'labels': labels},
                'spec': {
                    'runtimeClassName': 'kata-qemu-runtime-rs',
                    'restartPolicy': 'Never',
                    'automountServiceAccountToken': False,
                    'securityContext': {
                        'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
                        'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'},
                    },
                    'containers': [{
                        'name': 'agent-server', 'image': image, 'imagePullPolicy': 'IfNotPresent',
                        'workingDir': '/home/openhands',
                        'command': ['python', '-m', 'openhands.agent_server', '--host', '0.0.0.0', '--port', '8000'],
                        'env': [
                            {'name': 'OPENHANDS_SUPPRESS_BANNER', 'value': '1'},
                            {'name': 'LITELLM_LOCAL_MODEL_COST_MAP', 'value': 'True'},
                        ],
                        'envFrom': [{'secretRef': {'name': 'agent-session'}}],
                        'ports': [{'name': 'agent-api', 'containerPort': 8000}],
                        'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                        'resources': {
                            'requests': {'cpu': '250m', 'memory': '768Mi', 'ephemeral-storage': '512Mi'},
                            'limits': {'cpu': '2', 'memory': '2Gi', 'ephemeral-storage': '2Gi'},
                        },
                        'readinessProbe': {'httpGet': {'path': '/health', 'port': 8000},
                                           'periodSeconds': 5, 'failureThreshold': 30},
                        'volumeMounts': [
                            {'name': 'data', 'mountPath': '/home/openhands/workspace', 'subPath': 'workspace'},
                            {'name': 'data', 'mountPath': '/home/openhands/.openhands', 'subPath': 'openhands'},
                        ],
                    }],
                    'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'workspace-data'}}],
                },
            },
        },
    }
