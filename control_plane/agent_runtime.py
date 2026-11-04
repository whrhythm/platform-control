"""Disposable command compute. This module never mounts an entire personal volume."""
import re
from copy import deepcopy

from .workspace_files import WorkspaceFiles


def command_job(task, environment, binding, operation_id, command, settings):
    project = task.config['project_path']
    WorkspaceFiles._parts(project)
    namespace = 'codechat-user-' + environment.identity_hash[:40]
    if binding.namespace_name != namespace or not binding.pvc_uid:
        raise ValueError('agent_volume_requires_review')
    if not re.fullmatch(r'[0-9a-f-]{36}', operation_id):
        raise ValueError('agent_operation_invalid')
    if not settings.agent_command_image or '@sha256:' not in settings.agent_command_image:
        raise ValueError('agent_command_image_unpinned')
    labels = {'codechat.io/agent-task': task.id, 'codechat.io/agent-operation': operation_id,
              'codechat.io/user-hash': environment.identity_hash, 'codechat.io/agent-command': 'true'}
    return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {
        'name': 'agent-' + operation_id, 'namespace': namespace, 'labels': labels},
        'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': settings.agent_command_seconds,
                 'ttlSecondsAfterFinished': 300, 'template': {'metadata': {'labels': deepcopy(labels)}, 'spec': {
            'runtimeClassName': settings.runtime_class, 'restartPolicy': 'Never',
            'automountServiceAccountToken': False, 'enableServiceLinks': False,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
                                'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [{'name': 'command', 'image': settings.agent_command_image,
                'command': ['/bin/sh', '-c', 'mkdir -p .cache/home .cache/pip .cache/npm .cache/tmp && ' + command], 'workingDir': '/project',
                'env': [{'name': key, 'value': value} for key, value in {
                    'HOME': '/project/.cache/home', 'PIP_CACHE_DIR': '/project/.cache/pip',
                    'PIP_INDEX_URL': settings.workspace_pip_index_url or 'https://pypi.org/simple',
                    'npm_config_cache': '/project/.cache/npm', 'XDG_CACHE_HOME': '/project/.cache',
                    'TMPDIR': '/project/.cache/tmp', 'PYTHONDONTWRITEBYTECODE': '1'}.items()],
                'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                              'limits': {'cpu': settings.agent_command_cpu, 'memory': settings.agent_command_memory}},
                'securityContext': {'readOnlyRootFilesystem': True, 'allowPrivilegeEscalation': False,
                                    'capabilities': {'drop': ['ALL']}},
                'volumeMounts': [{'name': 'project', 'mountPath': '/project', 'subPath': 'files/' + project}]}],
            'volumes': [{'name': 'project', 'persistentVolumeClaim': {'claimName': 'workspace-data'}}]}}}}


def owned_job(actual, task_id, operation_id, uid=None):
    meta = actual.get('metadata', {})
    labels = meta.get('labels', {})
    if (not meta.get('uid') or (uid and meta['uid'] != uid) or
            meta.get('name') != 'agent-' + operation_id or
            labels.get('codechat.io/agent-task') != task_id or
            labels.get('codechat.io/agent-operation') != operation_id):
        raise RuntimeError('agent_resource_requires_review')
    return meta['uid']


def attach_services(job, project_path, *, database_secret=None, object_secret=None):
    pod = job['spec']['template']['spec']
    container = pod['containers'][0]
    if database_secret:
        if not re.fullmatch('database-[0-9a-f]{24}', database_secret):
            raise ValueError('agent_database_invalid')
        container['env'].extend({'name': name, 'valueFrom': {'secretKeyRef': {'name': database_secret, 'key': name}}}
            for name in ('DATABASE_URL', 'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE', 'PGSSLROOTCERT'))
        pod['volumes'].append({'name': 'database-ca', 'secret': {'secretName': database_secret,
                                                              'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}})
        container['volumeMounts'].append({'name': 'database-ca', 'mountPath': '/etc/codechat-database', 'readOnly': True})
    if object_secret:
        if not re.fullmatch('objects-[0-9a-f]{24}', object_secret):
            raise ValueError('agent_object_storage_invalid')
        container['env'].extend({'name': name, 'valueFrom': {'secretKeyRef': {'name': object_secret, 'key': name}}}
            for name in ('S3_ENDPOINT_URL', 'S3_BUCKET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_DEFAULT_REGION', 'AWS_REGION'))
        container['env'].append({'name': 'CODECHAT_OBJECT_PREFIX', 'value': project_path + '/'})


def workspace_agent_objects(namespace, identity_hash):
    if namespace != 'codechat-user-' + identity_hash[:40] or not re.fullmatch('[0-9a-f]{64}', identity_hash):
        raise ValueError('agent_namespace_invalid')
    meta = {'name': 'agent-worker', 'namespace': namespace, 'labels': {
        'app.kubernetes.io/managed-by': 'platform-control', 'codechat.io/user-hash': identity_hash[:40]}}
    return [{'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': deepcopy(meta),
             'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'codechat-agent-jobs'},
             'subjects': [{'kind': 'ServiceAccount', 'name': 'agent-worker', 'namespace': 'platform-system'}]},
            {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': deepcopy(meta),
             'spec': {'podSelector': {'matchLabels': {'codechat.io/agent-command': 'true'}},
                      'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': [
                          {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                                   'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                           'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]},
                          {'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': [
                              '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10']}}],
                           'ports': [{'protocol': 'TCP', 'port': 443}, {'protocol': 'TCP', 'port': 80}]},
                          {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'postgres'}}}],
                           'ports': [{'protocol': 'TCP', 'port': 5432}]},
                          {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'minio'}}}],
                           'ports': [{'protocol': 'TCP', 'port': 9000}]}]}}]
