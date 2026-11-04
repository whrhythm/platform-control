"""Server-generated Kata build jobs and user application workloads."""
import base64
import json
from urllib.parse import urlsplit
from .application_resources import container_resources, plan_resources


WRITE_REVIEW_CODE = 'provider_write_requires_review'


def app_name(app):
    return 'app-' + app.id.replace('-', '')[:24]


def application_origin(app, settings):
    base = urlsplit(settings.application_origin)
    if base.scheme not in ('http', 'https') or not base.hostname or base.username or base.password:
        raise ValueError('A valid application origin is required')
    return base.scheme + '://' + app_name(app) + '.' + base.hostname + (':' + str(base.port) if base.port else '')


def labels(app, digest):
    return {'app.kubernetes.io/managed-by': 'platform-control', 'codechat.io/app-id': app.id,
            'codechat.io/user-hash': digest[:40]}


def build_objects(app, environment, registry, settings, credential, workspace_token, ca):
    if not 256 <= settings.build_scratch_mib <= 4096 or not settings.build_scratch_mib + 512 <= settings.build_memory_mib <= 8192:
        raise ValueError('Build memory must include scratch space and worker headroom')
    revision = app.revision.replace('-', '')
    name, secret = 'build-' + revision[:24], 'build-auth-' + revision[:24]
    owner = labels(app, environment.identity_hash)
    owner['codechat.io/component'] = 'build'
    metadata = {'name': name, 'namespace': 'codechat-builds', 'labels': owner,
                'annotations': {'codechat.io/revision': app.revision}}
    registry_host = urlsplit(settings.harbor_url).netloc
    target = f'{registry_host}/{registry.project_name}/{app_name(app)}:{revision}'
    config = {'auths': {registry_host: {'auth': base64.b64encode((registry.robot_name + ':' + credential).encode()).decode()}}}
    secret_object = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {**metadata, 'name': secret},
        'type': 'Opaque', 'stringData': {'config.json': json.dumps(config), 'workspace-token': workspace_token, 'ca.crt': ca,
            'buildkitd.toml': f'[registry."{registry_host}"]\n  ca = ["/credentials/ca.crt"]\n'}}
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': metadata,
        'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 900, 'ttlSecondsAfterFinished': settings.build_job_retention_seconds,
            'template': {'metadata': {'labels': owner}, 'spec': {
                'runtimeClassName': settings.runtime_class, 'automountServiceAccountToken': False,
                'restartPolicy': 'Never', 'securityContext': {'fsGroup': 10001},
                'hostAliases': [{'ip': '192.168.10.51', 'hostnames': ['harbor.k3s.local']}],
                'initContainers': [{'name': 'source', 'image': settings.workspace_service_image,
                    'command': ['python', '-m', 'control_plane.build_context'],
                    'env': [{'name': 'PROJECT_PATH', 'value': app.project_path},
                            {'name': 'CONTEXT_URL', 'value': f'https://workspace-files.codechat-user-{environment.identity_hash[:40]}.svc:8090/v1/context'}],
                    'securityContext': {'runAsUser': 10001, 'runAsGroup': 10001, 'runAsNonRoot': True,
                                        'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                    'resources': {'requests': {'cpu': '50m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}},
                    'volumeMounts': [{'name': 'context', 'mountPath': '/context'},
                                     {'name': 'credentials', 'mountPath': '/credentials', 'readOnly': True}]}],
                'containers': [{'name': 'build', 'image': settings.buildkit_image,
                    'command': ['/bin/sh', '-ec',
                        f'mount -t tmpfs -o size={settings.build_scratch_mib * 1024 ** 2},mode=0700 tmpfs /var/lib/buildkit\n'
                        'exec buildctl-daemonless.sh "$@"', 'buildkit-job'],
                    'args': ['build', '--frontend', 'dockerfile.v0', '--local', 'context=/context/src',
                             '--local', 'dockerfile=/context/src', '--output', f'type=image,name={target},push=true'],
                    'env': [{'name': 'DOCKER_CONFIG', 'value': '/credentials'},
                            {'name': 'SSL_CERT_FILE', 'value': '/credentials/ca.crt'},
                            {'name': 'BUILDKITD_FLAGS', 'value': '--oci-worker-snapshotter=native --config /credentials/buildkitd.toml'}],
                    # Privilege is confined to the dedicated Kata VM. No host paths/sockets or SA token.
                    'securityContext': {'privileged': True, 'runAsUser': 0},
                    'resources': {'requests': {'cpu': '250m', 'memory': '512Mi', 'ephemeral-storage': '1Gi'},
                                  'limits': {'cpu': '2', 'memory': f'{settings.build_memory_mib}Mi', 'ephemeral-storage': '2Gi'}},
                    'volumeMounts': [{'name': 'context', 'mountPath': '/context', 'readOnly': True},
                                     {'name': 'credentials', 'mountPath': '/credentials', 'readOnly': True},
                                     {'name': 'build', 'mountPath': '/var/lib/buildkit'}]}],
                'volumes': [{'name': 'credentials', 'secret': {'secretName': secret}},
                            {'name': 'context', 'emptyDir': {'sizeLimit': '128Mi'}},
                            {'name': 'build', 'emptyDir': {'sizeLimit': f'{settings.build_scratch_mib}Mi'}}],
            }}}}
    if settings.workspace_pip_index_url:
        index = urlsplit(settings.workspace_pip_index_url)
        if index.scheme != 'https' or not index.hostname or index.username or index.password or index.query or index.fragment:
            raise ValueError('A credential-free HTTPS package index is required')
        job['spec']['template']['spec']['containers'][0]['args'].extend([
            '--opt', 'build-arg:PIP_INDEX_URL=' + settings.workspace_pip_index_url])
    return job, secret_object, target


def application_objects(app, environment, settings, *, database_secret=None, object_secret=None, runtime_rights=None):
    name = app_name(app)
    namespace = 'codechat-user-' + environment.identity_hash[:40]
    owner = labels(app, environment.identity_hash)
    meta = {'name': name, 'namespace': namespace, 'labels': owner}
    host = urlsplit(application_origin(app, settings)).hostname
    objects = [
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': meta,
         'spec': {'selector': owner, 'ports': [{'name': 'http', 'port': 8080, 'targetPort': 8080}]}},
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': meta,
         'spec': {'podSelector': {'matchLabels': owner}, 'policyTypes': ['Ingress', 'Egress'],
            'ingress': [{'from': [
                {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'apisix'}}},
                {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'platform-system'}},
                 'podSelector': {'matchLabels': {'codechat.io/publisher': 'true'}}}],
                'ports': [{'protocol': 'TCP', 'port': 8080}]}],
            'egress': [{'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}}}],
                        'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}},
        {'apiVersion': 'apisix.apache.org/v2', 'kind': 'ApisixRoute', 'metadata': meta,
         'spec': {'ingressClassName': 'apisix', 'http': [{'name': 'app', 'match': {'hosts': [host], 'paths': ['/*']},
            'backends': [{'serviceName': name, 'servicePort': 8080}]}]}},
        {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': meta,
         'spec': {'replicas': 0 if app.desired_state != 'running' else 1, 'revisionHistoryLimit': 2, 'progressDeadlineSeconds': 180,
            'selector': {'matchLabels': owner}, 'template': {'metadata': {'labels': owner,
                'annotations': {'codechat.io/revision': app.revision}}, 'spec': {
                'runtimeClassName': settings.runtime_class, 'automountServiceAccountToken': False,
                'imagePullSecrets': [{'name': 'application-registry'}],
                'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
                                    'seccompProfile': {'type': 'RuntimeDefault'}},
                'containers': [{'name': 'app', 'image': app.image, 'ports': [{'containerPort': 8080}],
                    'resources': container_resources(runtime_rights or plan_resources({})),
                    'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                    'readinessProbe': {'httpGet': {'path': '/', 'port': 8080}, 'periodSeconds': 5}}],
            }}}},
    ]
    if database_secret:
        spec = objects[-1]['spec']['template']['spec']
        spec['containers'][0]['env'] = [{'name': key, 'valueFrom': {'secretKeyRef': {'name': database_secret, 'key': key}}}
            for key in ('DATABASE_URL', 'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE', 'PGSSLROOTCERT')]
        spec['containers'][0]['volumeMounts'] = [{'name': 'database-ca', 'mountPath': '/etc/codechat-database', 'readOnly': True}]
        spec['volumes'] = [{'name': 'database-ca', 'secret': {'secretName': database_secret,
                            'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}}]
        policy = next(obj for obj in objects if obj['kind'] == 'NetworkPolicy')
        policy['spec']['egress'].append({'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'postgres'}}}],
                                          'ports': [{'protocol': 'TCP', 'port': 5432}]})
    if object_secret:
        container = objects[-1]['spec']['template']['spec']['containers'][0]
        container.setdefault('env', []).extend([{'name': key, 'valueFrom': {'secretKeyRef': {'name': object_secret, 'key': key}}}
            for key in ('S3_ENDPOINT_URL', 'S3_BUCKET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_DEFAULT_REGION', 'AWS_REGION')])
        container['env'].append({'name': 'CODECHAT_OBJECT_PREFIX', 'value': app.project_path + '/'})
        policy = next(obj for obj in objects if obj['kind'] == 'NetworkPolicy')
        policy['spec']['egress'].append({'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'minio'}}}],
                                          'ports': [{'protocol': 'TCP', 'port': 9000}]})
    return objects


def object_path(namespace, kind, name=None):
    group, plural = {
        'Job': ('apis/batch/v1', 'jobs'), 'Secret': ('api/v1', 'secrets'),
        'Service': ('api/v1', 'services'), 'Deployment': ('apis/apps/v1', 'deployments'),
        'NetworkPolicy': ('apis/networking.k8s.io/v1', 'networkpolicies'),
        'ApisixRoute': ('apis/apisix.apache.org/v2', 'apisixroutes'),
    }[kind]
    return f'{group}/namespaces/{namespace}/{plural}' + ('/' + name if name else '')
