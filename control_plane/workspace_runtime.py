"""Restricted file service resources for an already verified personal volume."""
import re


def file_service_objects(namespace: str, identity_hash: str, image: str, token: str) -> list[dict]:
    if not re.fullmatch(r'[0-9a-f]{64}', identity_hash) or namespace != 'codechat-user-' + identity_hash[:40]:
        raise ValueError('Invalid workspace identity')
    if not re.fullmatch(r'[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}', image):
        raise ValueError('An immutable file service image is required')
    if len(token) < 32:
        raise ValueError('Missing workspace credential')
    labels = {'codechat.io/component': 'workspace-files', 'codechat.io/user-hash': identity_hash[:40],
              'app.kubernetes.io/managed-by': 'platform-control'}
    meta = lambda name: {'name': name, 'namespace': namespace, 'labels': labels}
    host = f'workspace-files.{namespace}.svc'
    return [
        {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': meta('workspace-files-auth'), 'type': 'Opaque', 'immutable': True,
         'stringData': {'WORKSPACE_SERVICE_TOKEN': token}},
        {'apiVersion': 'cert-manager.io/v1', 'kind': 'Certificate', 'metadata': meta('workspace-files'),
         'spec': {'secretName': 'workspace-files-tls', 'dnsNames': [host],
                  'issuerRef': {'name': 'internal-ca', 'kind': 'ClusterIssuer'}}},
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': meta('workspace-files'),
         'spec': {'type': 'ClusterIP', 'selector': labels, 'ports': [{'name': 'https', 'port': 8090, 'targetPort': 8090}]}},
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': meta('workspace-files-access'),
         'spec': {'podSelector': {'matchLabels': labels}, 'policyTypes': ['Ingress'],
                  'ingress': [{'from': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'platform-system'}},
                                         'podSelector': {'matchLabels': {'codechat.io/workspace-client': 'true'}}}],
                               'ports': [{'protocol': 'TCP', 'port': 8090}]}]}},
        {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': meta('workspace-files'),
         'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': labels},
                  'template': {'metadata': {'labels': labels}, 'spec': {
                      'runtimeClassName': 'kata-qemu-runtime-rs', 'automountServiceAccountToken': False,
                      'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
                                          'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
                      'initContainers': [{'name': 'prepare-directory', 'image': image,
                          'command': ['python', '-c', "from pathlib import Path; Path('/workspace/files').mkdir(mode=0o700, exist_ok=True)"],
                          'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                              'capabilities': {'drop': ['ALL']}},
                          'resources': {'requests': {'cpu': '25m', 'memory': '64Mi'},
                                        'limits': {'cpu': '250m', 'memory': '128Mi'}},
                          'volumeMounts': [{'name': 'data', 'mountPath': '/workspace'}]}],
                      'containers': [{'name': 'files', 'image': image,
                          'command': ['python', '-m', 'control_plane.workspace_file_service'],
                          'envFrom': [{'secretRef': {'name': 'workspace-files-auth'}}],
                          'env': [{'name': 'WORKSPACE_ROOT', 'value': '/workspace/files'},
                                  {'name': 'WORKSPACE_SERVICE_HOST', 'value': '0.0.0.0'},
                                  {'name': 'WORKSPACE_TLS_CERT', 'value': '/tls/tls.crt'},
                                  {'name': 'WORKSPACE_TLS_KEY', 'value': '/tls/tls.key'},
                                  {'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'}],
                          'ports': [{'containerPort': 8090, 'name': 'https'}],
                          'resources': {'requests': {'cpu': '50m', 'memory': '128Mi', 'ephemeral-storage': '64Mi'},
                                        'limits': {'cpu': '500m', 'memory': '256Mi', 'ephemeral-storage': '128Mi'}},
                          'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                              'capabilities': {'drop': ['ALL']}},
                          'readinessProbe': {'httpGet': {'path': '/healthz', 'port': 8090, 'scheme': 'HTTPS'},
                                             'periodSeconds': 5},
                          'volumeMounts': [{'name': 'data', 'mountPath': '/workspace'},
                                           {'name': 'tls', 'mountPath': '/tls', 'readOnly': True},
                                           {'name': 'tmp', 'mountPath': '/tmp'}]}],
                      'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'workspace-data'}},
                                  {'name': 'tls', 'secret': {'secretName': 'workspace-files-tls'}},
                                  {'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}}]}}}},
    ]


def resource_path(namespace: str, kind: str) -> str:
    if not re.fullmatch(r'codechat-user-[0-9a-f]{40}', namespace):
        raise ValueError('Invalid workspace namespace')
    prefix = {'Secret': 'api/v1', 'Service': 'api/v1', 'Certificate': 'apis/cert-manager.io/v1',
              'NetworkPolicy': 'apis/networking.k8s.io/v1', 'Deployment': 'apis/apps/v1'}[kind]
    plural = {'Secret': 'secrets', 'Service': 'services', 'Certificate': 'certificates',
              'NetworkPolicy': 'networkpolicies', 'Deployment': 'deployments'}[kind]
    return f'{prefix}/namespaces/{namespace}/{plural}'
