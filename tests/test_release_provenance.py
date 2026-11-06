from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from control_plane.models import AgentTask
from control_plane.models import UserApplication, UserEnvironment, UserRegistry, WorkspaceFileService, WorkspaceVolumeBinding
from control_plane.application_controller import Cluster, reconcile_application_iteration
from control_plane.release_provenance import freeze, public, build_source, application_provenance
from control_plane.settings import get_settings
from test_agent_workbench import workspace
from test_task_acceptance import setup_record, body, source_boundary


def confirmed(workspace):
    client, headers, _ = workspace
    task, url = setup_record(workspace)
    value = body()
    assert client.put(url, headers=headers, json=value).status_code == 200
    value.update(revision=1)
    value['criteria'][0]['result'] = 'passed'
    result = client.put(url, headers=headers, json=value)
    assert result.status_code == 200
    return task, url, value, result.json()


def test_freeze_scoped_content_is_immutable_and_requires_exact_build_hash(workspace):
    client, headers, _ = workspace
    task, url, value, accepted = confirmed(workspace)
    assert accepted['source_sha256'] == hashlib.sha256(b'synthetic build context').hexdigest()
    with client.session_factory() as db:
        record = freeze(db, task.environment_id, task.config['project_path'])
        assert freeze(db, 'another-user', task.config['project_path'])['candidate'] is None
        assert freeze(db, task.environment_id, 'other-project')['candidate'] is None
    record.update(revision='release-one', source_sha256=accepted['source_sha256'], image_digest='sha256:'+'a'*64)
    view = public(record, get_settings())
    assert view['acceptance_state'] == 'matched'
    assert view['acceptance']['criteria'][0]['result'] == 'passed'
    assert 'encrypted' not in str(view)
    value.update(revision=2, note='Updated after publication')
    assert client.put(url, headers=headers, json=value).status_code == 200
    assert public(record, get_settings()) == view
    changed = public({**record, 'source_sha256': 'f'*64}, get_settings())
    assert changed['acceptance_state'] == 'changed' and 'criteria' not in changed['acceptance']
    # Failure cleanup must continue showing the previously confirmed version.
    app = SimpleNamespace(resources={'provenance': {**record, 'revision': 'release-two', 'source_sha256': 'f'*64},
        'last_good': {'provenance': record}, 'cleanup': {'status': 'complete', 'runtime': 'previous_release'}})
    output = application_provenance(app, get_settings())
    assert output['release']['revision'] == 'release-one' and output['release']['acceptance_state'] == 'matched'
    assert output['attempt']['acceptance_state'] == 'changed'


def test_changed_source_requires_reset_and_unavailable_source_does_not_save(workspace, monkeypatch):
    client, headers, _ = workspace
    task, url, value, accepted = confirmed(workspace)
    monkeypatch.setattr('control_plane.task_acceptance.call_files', lambda *a, **kw: httpx.Response(200, content=b'new source'))
    value.update(revision=2, note='note only')
    assert client.put(url, headers=headers, json=value).json()['source_sha256'] == accepted['source_sha256']
    value.update(revision=3)
    value['criteria'][0]['result'] = 'changes_requested'
    result = client.put(url, headers=headers, json=value)
    assert result.status_code == 409 and result.json()['detail'] == 'acceptance_source_changed'
    value['criteria'][0]['result'] = 'pending'
    assert client.put(url, headers=headers, json=value).json()['source_sha256'] is None
    value.update(revision=4)
    value['criteria'][0]['result'] = 'passed'
    def unavailable(*a, **kw): raise HTTPException(503, 'private provider detail')
    monkeypatch.setattr('control_plane.task_acceptance.call_files', unavailable)
    result = client.put(url, headers=headers, json=value)
    assert result.status_code == 503 and result.json()['detail'] == 'acceptance_source_unavailable'
    assert client.get(url, headers=headers).json()['revision'] == 4
    monkeypatch.setattr('control_plane.task_acceptance.call_files', lambda *a, **kw: httpx.Response(200, content=b'new source'))
    assert client.put(url, headers=headers, json=value).json()['source_sha256'] == hashlib.sha256(b'new source').hexdigest()


def test_legacy_acceptance_is_never_retroactively_bound(workspace):
    client, headers, _ = workspace
    task, url, _, _ = confirmed(workspace)
    with client.session_factory.begin() as db:
        row = db.get(AgentTask, task.id)
        state = deepcopy(row.resources)
        del state['acceptance']['source_sha256']
        row.resources = state
    with client.session_factory() as db:
        record = freeze(db, task.environment_id, task.config['project_path'])
    assert public({**record, 'source_sha256': 'a'*64}, get_settings())['acceptance_state'] == 'unbound'
    assert application_provenance(SimpleNamespace(resources={}), get_settings()) == {'attempt': None, 'release': None}


@pytest.mark.parametrize('proof', [True, False])
def test_controller_observes_build_source_before_deploying(workspace, proof):
    client, _, _ = workspace
    task, _, _, accepted = confirmed(workspace)
    with client.session_factory.begin() as db:
        env = db.get(UserEnvironment, task.environment_id)
        namespace = 'codechat-user-' + env.identity_hash[:40]
        db.add_all([
            WorkspaceVolumeBinding(environment_id=env.id, namespace_name=namespace, namespace_uid='namespace',
                pvc_uid='pvc', pv_name='pv', pv_uid='pv', volume_handle='volume'),
            WorkspaceFileService(environment_id=env.id, encrypted_token='unused', status='ready'),
            UserRegistry(environment_id=env.id, project_name='demo', robot_name='demo', encrypted_secret='unused', status='ready', pull_secret_uid='pull'),
        ])
        app = UserApplication(environment_id=env.id, project_path='demo', status='building', resources={
            'job': {'name': 'build-test', 'uid': 'owned-job'}, 'provenance': freeze(db, env.id, 'demo')})
        db.add(app); db.flush(); identifier = app.id
    calls = []
    def provider(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith('/jobs/build-test'):
            return httpx.Response(200, json={'metadata': {'uid': 'owned-job'}, 'status': {'conditions': [{'type':'Complete','status':'True'}]}})
        if request.url.path.endswith('/pods'):
            pod = {'metadata': {'labels': {'codechat.io/app-id': identifier}, 'ownerReferences': [{'kind':'Job','uid':'owned-job'}]},
                'status': {'initContainerStatuses': [{'name':'source','state':{'terminated':{'exitCode':0,
                    'message':json.dumps({'source_sha256':accepted['source_sha256']})}}}]}}
            return httpx.Response(200, json={'items': [pod] if proof else []})
        if '/artifacts/' in request.url.path:
            return httpx.Response(200, json={'digest':'sha256:'+'a'*64})
        return httpx.Response(501)
    with httpx.Client(base_url='https://provider.test/', transport=httpx.MockTransport(provider)) as remote:
        reconcile_application_iteration(client.session_factory, identifier, Cluster(remote), remote, get_settings())
    with client.session_factory() as db:
        result = db.get(UserApplication, identifier)
        if proof:
            assert result.status == 'deploying'
            assert result.resources['provenance']['source_sha256'] == accepted['source_sha256']
            assert result.resources['provenance']['image_digest'] == 'sha256:'+'a'*64
        else:
            assert result.status == 'failed' and result.error_code == 'build_source_verify_failed'
            assert result.image is None
    assert all(method == 'GET' for method, _ in calls)


@pytest.mark.parametrize('mode', ['valid', 'wrong-owner', 'failed-init', 'build-container', 'invalid-json', 'duplicate', 'missing'])
def test_build_evidence_requires_one_owned_successful_source_container(mode):
    pod = {'metadata': {'labels': {'codechat.io/app-id': 'app'}, 'ownerReferences': [{'kind': 'Job', 'uid': 'job'}]},
           'status': {'initContainerStatuses': [{'name': 'source', 'state': {'terminated': {'exitCode': 0,
              'message': json.dumps({'source_sha256': 'a'*64})}}}]}}
    if mode == 'wrong-owner': pod['metadata']['ownerReferences'][0]['uid'] = 'another-job'
    if mode == 'failed-init': pod['status']['initContainerStatuses'][0]['state']['terminated']['exitCode'] = 1
    if mode == 'build-container': pod['status']['initContainerStatuses'][0]['name'] = 'build'
    if mode == 'invalid-json': pod['status']['initContainerStatuses'][0]['state']['terminated']['message'] = 'secret malformed'
    pods = [] if mode == 'missing' else [pod, pod] if mode == 'duplicate' else [pod]
    cluster = SimpleNamespace(get=lambda _: {'items': pods})
    result = build_source(cluster, SimpleNamespace(id='app'), {'name': 'build-test', 'uid': 'job'})
    assert result == ('a'*64 if mode == 'valid' else None)
