"""Trusted tool gateway. User commands run only in recorded Kata Jobs."""
import hashlib
import json
import time
from datetime import timedelta
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from .agent_runtime import command_job, owned_job, attach_services
from .agent_tasks import append_event, now, unseal
from .application_resources import container_resources
from .auth import Principal
from .models import AgentTask, AuditEvent, UserEnvironment, WorkspaceVolumeBinding, ProjectDatabase, UserObjectStorage
from .workspace_files import WorkspaceFiles
from .workspace_gateway import call_files


class TaskStopped(RuntimeError):
    pass


class TaskTools:
    def __init__(self, sessions, task_id, cluster, settings, api):
        self.sessions, self.task_id, self.cluster, self.settings, self.api = sessions, task_id, cluster, settings, api
        self.stopped = False
        self.stop_reason = 'agent_task_stopped'

    def current(self, db, *, lock=False):
        query = select(AgentTask).where(AgentTask.id == self.task_id)
        task = db.scalar(query.with_for_update() if lock else query)
        env = db.get(UserEnvironment, task.environment_id)
        from .accounts import allowed
        if not allowed(db, env.issuer, env.subject):
            raise TaskStopped('account_disabled')
        if self.stopped or task.status not in ('running', 'awaiting_approval') or env.status in ('suspended', 'deleted'):
            raise TaskStopped(self.stop_reason)
        return task, env

    def event(self, kind, **data):
        with self.sessions.begin() as db:
            task, _ = self.current(db, lock=True)
            append_event(db, task, self.settings, kind, **data)

    def progress(self, kind, identifier, **data):
        """Replace a bounded encrypted snapshot, not a new event for every token/log poll."""
        if kind not in ('assistant', 'command_progress', 'model_progress'):
            raise ValueError('agent_progress_invalid')
        with self.sessions.begin() as db:
            task, _ = self.current(db, lock=True)
            events = unseal(task.encrypted_events, self.settings)
            current = next((e for e in events if e['kind'] == kind and e.get('stream_id') == identifier), None)
            if current is None:
                append_event(db, task, self.settings, kind, stream_id=identifier, **data)
                return
            current.update(data)
            current['at'] = now().isoformat()
            from .agent_tasks import seal
            task.encrypted_events, task.updated_at = seal(events, self.settings), now()

    def authorize(self, operation, arguments):
        operation_id = str(uuid4())
        with self.sessions.begin() as db:
            task, env = self.current(db, lock=True)
            if task.config['strategy'] != 'implement' and operation != 'mcp_read':
                raise TaskStopped('agent_strategy_read_only')
            digest = hashlib.sha256(json.dumps([task.config, operation, arguments], sort_keys=True).encode()).hexdigest()
            required = (task.config['approval'] == 'strict'
                        or operation in ('publish', 'start', 'stop', 'delete', 'database', 'mcp_read', 'mcp_write')
                        or (operation == 'command' and (arguments.get('database') or arguments.get('objects'))))
            deadline = now() + timedelta(seconds=self.settings.agent_approval_seconds)
            task.pending = {'id': operation_id, 'digest': digest, 'expires_at': deadline.isoformat(),
                            'decision': None if required else 'approve'}
            task.status = 'awaiting_approval' if required else 'running'
            append_event(db, task, self.settings, 'approval' if required else 'authorized',
                         operation_id=operation_id, operation=operation, arguments=arguments,
                         expires_at=deadline.isoformat())
            db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=env.issuer, subject=env.subject,
                             action='agent.operation.proposed', target_id=task.id + '/' + operation_id))
        while True:
            with self.sessions.begin() as db:
                task, _ = self.current(db, lock=True)
                pending = task.pending or {}
                if pending.get('id') != operation_id or pending.get('digest') != digest:
                    raise TaskStopped('agent_approval_changed')
                if pending.get('decision') == 'reject' or now() >= deadline:
                    self.stopped = True
                    self.stop_reason = 'agent_approval_rejected' if pending.get('decision') else 'agent_approval_expired'
                    raise TaskStopped(self.stop_reason)
                if pending.get('decision') == 'approve':
                    task.status, task.pending = 'running', None
                    task.resources = {**task.resources, 'operation': {'id': operation_id, 'kind': operation, 'state': 'authorized'}}
                    return operation_id
            time.sleep(0.5)

    def file(self, operation, path='', content='', etag=''):
        if operation not in ('list', 'read', 'write', 'mkdir'):
            raise ValueError('agent_tool_invalid')
        if path:
            WorkspaceFiles._parts(path)
        elif operation != 'list':
            raise ValueError('agent_path_required')
        if len(content.encode()) > 256 * 1024:
            raise ValueError('agent_file_too_large')
        with self.sessions() as db:
            task, env = self.current(db)
            full_path = task.config['project_path'] + ('/' + path if path else '')
            principal = Principal(env.issuer, env.subject)
        if operation in ('write', 'mkdir'):
            self.authorize(operation, {'path': path, 'content': content, 'etag': etag})
        method, endpoint = {'list': ('GET', '/v1/directories'), 'read': ('GET', '/v1/file'),
                            'write': ('PUT', '/v1/file'), 'mkdir': ('POST', '/v1/directories')}[operation]
        try:
            with self.sessions() as db:
                self.current(db)
                response = call_files(db, principal, self.settings, method, endpoint, full_path,
                                      body=content.encode() if operation == 'write' else None,
                                      overwrite=bool(etag), if_match=etag)
        except HTTPException as error:
            if operation in ('write', 'mkdir') and error.status_code >= 500:
                self.stopped = True
                self.stop_reason = 'agent_write_requires_review'
                raise TaskStopped('agent_write_requires_review') from None
            return json.dumps({'error': 'file_operation_failed', 'status': error.status_code})
        if operation == 'read' and len(response.content) > 32768:
            return json.dumps({'error': 'file_too_large_for_agent_read', 'path': path,
                               'message': 'Use a bounded command to inspect sections; never replace from partial content.'})
        result = response.json() if operation == 'list' else {'path': path, 'etag': response.headers.get('etag'),
                                                              'content': response.text if operation == 'read' else ''}
        self.event('file', operation=operation, path=path)
        return json.dumps(result, ensure_ascii=False)

    def command(self, command, *, database=False, objects=False):
        if not command or len(command) > 16384:
            raise ValueError('agent_command_invalid')
        operation_id = self.authorize('command', {'command': command, 'database': database, 'objects': objects})
        self.progress('command_progress', operation_id, phase='preparing', output='')
        sensitive = []
        with self.sessions.begin() as db:
            task, env = self.current(db, lock=True)
            binding = db.get(WorkspaceVolumeBinding, env.id)
            desired = command_job(task, env, binding, operation_id, command, self.settings)
            database_secret, object_secret = None, None
            if database:
                row = db.scalar(select(ProjectDatabase).where(ProjectDatabase.environment_id == env.id,
                    ProjectDatabase.project_path == task.config['project_path'], ProjectDatabase.status == 'ready'))
                if row is None:
                    raise TaskStopped('agent_database_not_ready')
                from .project_databases import connection_profile, names
                database_secret = names(row)[2]
                profile = connection_profile(row, self.settings)
                sensitive.extend([profile['DATABASE_URL'], profile['PGPASSWORD']])
            if objects:
                row = db.get(UserObjectStorage, env.id)
                if row is None or row.status != 'ready':
                    raise TaskStopped('agent_objects_not_ready')
                from .object_storage import connection_profile, secret_name
                object_secret = secret_name(row)
                profile = connection_profile(row, self.settings)
                sensitive.extend([profile['AWS_SECRET_ACCESS_KEY'], profile['AWS_ACCESS_KEY_ID']])
            attach_services(desired, task.config['project_path'], database_secret=database_secret, object_secret=object_secret)
            from .plan_quotas import active_quotas
            quotas = task.config.get('quota') or active_quotas(db, env.id)
            desired['spec']['template']['spec']['containers'][0]['resources'] = container_resources({
                'application_cpu_millicores': quotas['agent_cpu_millicores'],
                'application_memory_mib': quotas['agent_memory_mib']})
            record = {'name': desired['metadata']['name'], 'namespace': binding.namespace_name,
                      'operation_id': operation_id, 'uid': None}
            task.resources = {**task.resources, 'job': record, 'cleanup': {'status': 'pending'}}
        path = f"apis/batch/v1/namespaces/{record['namespace']}/jobs/{record['name']}"
        pvc = self.cluster.get(f"api/v1/namespaces/{record['namespace']}/persistentvolumeclaims/workspace-data")
        if not pvc or pvc.get('metadata', {}).get('uid') != binding.pvc_uid:
            raise TaskStopped('agent_volume_requires_review')
        # An unknown create is never retried. Observe the same name, then require review if absent.
        try:
            actual = self.cluster.create(desired)
        except Exception:
            actual = self.cluster.get(path)
            if actual is None:
                self.stopped = True
                self.stop_reason = 'agent_write_requires_review'
                raise TaskStopped('agent_write_requires_review') from None
        uid = owned_job(actual, self.task_id, operation_id)
        with self.sessions.begin() as db:
            task = db.scalar(select(AgentTask).where(AgentTask.id == self.task_id).with_for_update())
            task.resources = {**task.resources, 'job': {**record, 'uid': uid}}
        deadline = time.monotonic() + desired['spec']['activeDeadlineSeconds'] + 90
        while time.monotonic() < deadline:
            with self.sessions() as db:
                self.current(db)
            try:
                actual = self.cluster.get(path)
            except httpx.HTTPError:
                time.sleep(2)
                continue
            if actual is None:
                raise TaskStopped('agent_command_result_unknown')
            owned_job(actual, self.task_id, operation_id, uid)
            conditions = actual.get('status', {}).get('conditions', [])
            self.command_progress(record, uid, sensitive)
            if any(value.get('type') in ('Complete', 'Failed') and value.get('status') == 'True' for value in conditions):
                break
            time.sleep(2)
        else:
            raise TaskStopped('agent_command_timed_out')
        output = ''
        exit_code = None
        pods = self.cluster.get(f"api/v1/namespaces/{record['namespace']}/pods?labelSelector=job-name%3D{record['name']}")
        for pod in (pods or {}).get('items', []):
            meta = pod.get('metadata', {})
            if not any(owner.get('uid') == uid and owner.get('kind') == 'Job' for owner in meta.get('ownerReferences', [])):
                continue
            states = pod.get('status', {}).get('containerStatuses', [])
            exit_code = next((s['state']['terminated']['exitCode'] for s in states if 'terminated' in s.get('state', {})), None)
            result = self.cluster.client.get(f"api/v1/namespaces/{record['namespace']}/pods/{meta['name']}/log",
                                             params={'container': 'command', 'limitBytes': 32768, 'tailLines': 250})
            if result.status_code == 200:
                output = result.text
        for secret in sensitive:
            output = output.replace(secret, '[redacted]')
        self.event('terminal', operation_id=operation_id, exit_code=exit_code, output=output,
                   success=any(c.get('type') == 'Complete' and c.get('status') == 'True' for c in conditions))
        cleanup(self.sessions, self.task_id, self.cluster)
        return json.dumps({'exit_code': exit_code, 'output': output}, ensure_ascii=False)

    def command_progress(self, record, uid, sensitive):
        """Observe only owned pods; transient log-read failures never replay the command."""
        try:
            pods = self.cluster.get(f"api/v1/namespaces/{record['namespace']}/pods?labelSelector=job-name%3D{record['name']}")
            for pod in (pods or {}).get('items', []):
                meta = pod.get('metadata', {})
                if not any(owner.get('uid') == uid and owner.get('kind') == 'Job' for owner in meta.get('ownerReferences', [])):
                    continue
                phase = pod.get('status', {}).get('phase')
                if phase not in ('Running', 'Succeeded', 'Failed'):
                    continue
                result = self.cluster.client.get(f"api/v1/namespaces/{record['namespace']}/pods/{meta['name']}/log",
                    params={'container': 'command', 'limitBytes': 32768, 'tailLines': 100}, timeout=3)
                if result.status_code != 200:
                    continue
                output = result.text
                for secret in sensitive:
                    output = output.replace(secret, '[redacted]')
                    # A still-growing log may end part-way through a credential.
                    for size in range(min(len(secret) - 1, len(output)), 0, -1):
                        if output.endswith(secret[:size]):
                            output = output[:-size]
                            break
                self.progress('command_progress', record['operation_id'],
                    phase={'Running': 'running', 'Succeeded': 'completed', 'Failed': 'failed'}[phase],
                    output=output[-8192:])
        except httpx.HTTPError:
            return

    def services(self, action):
        with self.sessions() as db:
            task, _ = self.current(db)
            project = task.config['project_path']
        if action == 'database':
            self.authorize('database', {'project_path': project})
            try:
                response = self.api.post('/agent/v1/databases', json={'project_path': project})
                if response.status_code >= 500:
                    raise httpx.TransportError('unknown')
            except httpx.HTTPError:
                self.stopped = True
                self.stop_reason = 'agent_write_requires_review'
                raise TaskStopped('agent_write_requires_review') from None
        elif action == 'databases':
            response = self.api.get('/agent/v1/databases')
        else:
            response = self.api.get('/agent/v1/object-storage')
        if response.status_code not in (200, 202):
            return json.dumps({'error': 'service_unavailable', 'status': response.status_code})
        result = response.json()
        if action == 'databases':
            result = {'databases': [row for row in result['databases'] if row['project_path'] == project]}
        self.event('service', operation=action, text='已查询或提交工程服务，请以服务实际状态为准。')
        return json.dumps(result, ensure_ascii=False)

    def applications(self, action='list', application_id=''):
        if action not in ('list', 'publish', 'start', 'stop', 'delete'):
            raise ValueError('agent_tool_invalid')
        with self.sessions() as db:
            task, _ = self.current(db)
            project = task.config['project_path']
        listing = self.api.get('/agent/v1/apps')
        listing.raise_for_status()
        apps = [app for app in listing.json()['apps'] if app['project_path'] == project]
        if action == 'list':
            return json.dumps({'apps': apps}, ensure_ascii=False)
        if action != 'publish' and not any(app['id'] == application_id for app in apps):
            raise ValueError('agent_application_not_in_project')
        operation_id = self.authorize(action, {'project_path': project, 'application_id': application_id})
        try:
            response = self.api.post('/agent/v1/apps' if action == 'publish' else f'/agent/v1/apps/{application_id}/{action}',
                params={'path': project} if action == 'publish' else {},
                headers={'X-CodeChat-Task-Id': hashlib.sha256(operation_id.encode()).hexdigest()})
        except httpx.HTTPError:
            self.stopped = True
            self.stop_reason = 'agent_write_requires_review'
            raise TaskStopped('agent_write_requires_review') from None
        if response.status_code >= 500:
            self.stopped = True
            self.stop_reason = 'agent_write_requires_review'
            raise TaskStopped('agent_write_requires_review')
        if response.status_code not in (200, 202):
            return json.dumps({'error': 'application_request_rejected', 'status': response.status_code})
        result = response.json()
        self.event('application', operation=action, application=result)
        return json.dumps(result, ensure_ascii=False)


def cleanup(sessions, task_id, cluster):
    with sessions() as db:
        task = db.get(AgentTask, task_id)
        job = task.resources.get('job')
    if not job:
        return
    path = f"apis/batch/v1/namespaces/{job['namespace']}/jobs/{job['name']}"
    actual = cluster.get(path)
    if actual:
        uid = owned_job(actual, task_id, job['operation_id'], job.get('uid'))
        cluster.delete(path, uid, foreground=True)
        # Never launch the next command while previous compute is still terminating.
        for _ in range(30):
            if cluster.get(path) is None:
                break
            time.sleep(1)
        else:
            raise TaskStopped('agent_cleanup_pending')
    with sessions.begin() as db:
        task = db.scalar(select(AgentTask).where(AgentTask.id == task_id).with_for_update())
        task.resources = {**task.resources, 'job': None, 'cleanup': {'status': 'complete'}}
