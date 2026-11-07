from uuid import uuid4

import pytest
from sqlalchemy import select

from control_plane.knowledge import knowledge_usage
from control_plane.knowledge_format import prepare
from control_plane.knowledge_worker import run_once
from control_plane.models import ProjectKnowledge, AIProjectBinding
from control_plane.settings import get_settings
from test_ai_projects import setup, OWNER, PREFIX
from test_project_versions import versions


class Store:
    def __init__(self):
        self.rows = {}
        self.fail = False
        self.lost_reply = False
        self.puts = 0

    def call(self, method, scope, revision, payload=None, **kwargs):
        if self.fail:
            raise TimeoutError('private submitted text must not be persisted')
        key = (scope, revision)
        if method == 'DELETE':
            self.rows.pop(key, None)
            return {'deleted': True}
        if method == 'GET':
            if key not in self.rows:
                raise LookupError('not_found')
            return self.rows[key]
        self.puts += 1
        digest, size, index, chunks = prepare(payload['files'])
        self.rows[key] = dict(digest=digest, source_bytes=size, index_bytes=index, chunks=len(chunks))
        if self.lost_reply:
            raise TimeoutError('response lost after commit')
        return self.rows[key]


def initialized(versions, monkeypatch):
    client, auth, root = setup(versions, monkeypatch)
    monkeypatch.setenv('PLATFORM_KNOWLEDGE_ENABLED', 'true')
    monkeypatch.setattr('control_plane.knowledge.call_files', versions[4])
    get_settings.cache_clear()
    value = client.post(PREFIX, headers=auth, json={'request_id': str(uuid4()),
        'name': '知识', 'purpose': '合成测试', 'native_owner_id': OWNER}).json()
    client.post(PREFIX+'/'+value['project']['id']+'/confirm', headers=auth,
        json={'native_owner_id': OWNER, 'agent_id': value['agent_id']})
    return client, auth, value['project']['id']


def test_intents_recovery_scopes_deletion_and_metering(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    url = PREFIX+'/'+project+'/knowledge'
    payload = {'request_id': str(uuid4()), 'native_owner_id': OWNER,
               'files': [{'name': '政策.md', 'content': '# 演示政策\n换货期限七天。'}]}
    result = client.post(url, headers=auth, json=payload)
    assert result.status_code == 202, result.text
    row = result.json()
    assert row['state'] == 'queued'
    assert client.post(url, headers=auth, json=payload).json()['id'] == row['id']
    with client.session_factory() as db:
        saved = db.get(ProjectKnowledge, row['id'])
        assert '换货' not in saved.encrypted_source
        used = knowledge_usage(db, saved.environment_id)
        assert used == row['source_bytes']+row['index_bytes']
    assert client.get(url, headers={**auth, 'X-CodeChat-Subject': 'foreign'}).status_code == 404
    assert client.post(url, headers=auth, json={**payload, 'native_owner_id': 'b'*24}).status_code == 403
    assert client.post(url, headers=auth, json={**payload, 'files': [{'name': 'x.txt', 'content': 'changed'}]}).status_code == 409
    store = Store()
    store.lost_reply = True
    run_once(client.session_factory, get_settings(), store)
    failed = client.get(url, headers=auth).json()['revisions'][0]
    assert failed['state'] == 'failed' and failed['error_code'] == 'knowledge_index_failed'
    assert client.post(url+'/'+row['id']+'/retry', headers=auth).status_code == 200
    run_once(client.session_factory, get_settings(), store)
    listing = client.get(url, headers=auth).json()
    assert listing['active_revision_id'] == row['id']
    assert store.puts == 1  # Verified committed result; no duplicate embedding after lost reply.
    source_url = url+'/'+row['id']+'/source'
    source = client.get(source_url, headers=auth, params={'name':'政策.md'})
    assert source.status_code == 200 and source.json()['content'] == payload['files'][0]['content']
    assert client.get(source_url, headers={**auth,'X-CodeChat-Subject':'foreign'}, params={'name':'政策.md'}).status_code == 404
    assert client.get(source_url, headers=auth, params={'name':'../policy.md'}).status_code == 404
    capacity = client.get('/internal/v1/workspace/capacity', headers=auth).json()
    assert next(c for c in capacity['storage']['components'] if c['component']=='knowledge')['used_bytes'] == used
    # Removal hides the active revision immediately but retains capacity until confirmed cleanup.
    client.post(url+'/'+row['id']+'/remove', headers=auth)
    assert client.get(url, headers=auth).json()['active_revision_id'] is None
    assert client.get(source_url, headers=auth, params={'name':'政策.md'}).status_code == 404
    store.fail = True
    run_once(client.session_factory, get_settings(), store)
    with client.session_factory() as db:
        saved = db.get(ProjectKnowledge, row['id'])
        assert saved.state == 'deleting' and knowledge_usage(db, saved.environment_id) == used
        saved.lease_until = None
        db.commit()
    store.fail = False
    run_once(client.session_factory, get_settings(), store)
    with client.session_factory() as db:
        saved = db.get(ProjectKnowledge, row['id'])
        assert saved.state == 'deleted' and not saved.encrypted_source
        assert knowledge_usage(db, saved.environment_id) == 0


def test_update_preserves_old_revision_and_invalid_inputs(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    url = PREFIX+'/'+project+'/knowledge'
    store = Store()
    revisions = []
    for content in ['七天', '十四天']:
        row = client.post(url, headers=auth, json={'request_id': str(uuid4()), 'native_owner_id': OWNER,
            'files': [{'name': 'policy.txt', 'content': content}]}).json()
        revisions.append(row['id'])
        run_once(client.session_factory, get_settings(), store)
    assert client.get(url, headers=auth).json()['active_revision_id'] == revisions[1]
    assert len(store.rows) == 2
    assert all(r['state'] == 'ready' for r in client.get(url, headers=auth).json()['revisions'])
    for name in ['../policy.md', 'a/b.md', 'unsafe.html']:
        result = client.post(url, headers=auth, json={'request_id': str(uuid4()), 'native_owner_id': OWNER,
            'files': [{'name': name, 'content': 'body'}]})
        assert result.status_code == 400
    monkeypatch.setenv('PLATFORM_KNOWLEDGE_FILE_BYTES', '1024')
    get_settings.cache_clear()
    result = client.post(url, headers=auth, json={'request_id': str(uuid4()), 'native_owner_id': OWNER,
        'files': [{'name': 'big.md', 'content': 'x'*1025}]})
    assert result.status_code == 400


def test_source_limits_and_deterministic_logical_bytes():
    files = [{'name': '演示.md', 'content': '资料\n'*220}]
    assert prepare(files) == prepare(files)
    digest, source, index, chunks = prepare(files)
    assert source == len(files[0]['content'].encode()) and index > 512*4*len(chunks)
    with pytest.raises(ValueError):
        prepare(files, max_chunks=1)


def test_search_rechecks_removal_and_never_queries_foreign_identity(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    url = PREFIX+'/'+project+'/knowledge'
    row = client.post(url, headers=auth, json={'request_id': str(uuid4()), 'native_owner_id': OWNER,
        'files': [{'name': 'policy.md', 'content': 'Synthetic seven day policy'}]}).json()
    run_once(client.session_factory, get_settings(), Store())
    calls = []

    class SearchStore:
        def __init__(self, settings):
            pass

        def call(self, method, scope, revision, payload, **kwargs):
            calls.append((scope, revision))
            with client.session_factory() as db:
                saved = db.get(ProjectKnowledge, revision)
                saved.state = 'deleting'
                db.commit()
            return {'sources': [{'content': 'must not escape after removal'}]}

    monkeypatch.setattr('control_plane.knowledge.KnowledgeClient', SearchStore)
    body = {'native_owner_id': OWNER, 'query': 'policy'}
    assert client.post(url+'/search', headers={**auth, 'X-CodeChat-Subject': 'foreign'}, json=body).status_code == 404
    assert client.post(url+'/search', headers=auth, json={**body, 'native_owner_id': 'b'*24}).status_code == 403
    assert calls == []
    result = client.post(url+'/search', headers=auth, json=body)
    assert result.status_code == 409
    assert 'must not escape' not in result.text
    assert calls == [(project, row['id'])]


def test_late_retry_cannot_rewind_newer_active_knowledge(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    url = PREFIX+'/'+project+'/knowledge'
    store = Store()
    def create(content):
        return client.post(url, headers=auth, json={'request_id': str(uuid4()), 'native_owner_id': OWNER,
            'files': [{'name': 'policy.md', 'content': content}]}).json()['id']
    old = create('old policy')
    store.fail = True
    run_once(client.session_factory, get_settings(), store)
    new = create('new policy')
    store.fail = False
    run_once(client.session_factory, get_settings(), store)
    assert client.post(url+'/'+old+'/retry', headers=auth).status_code == 200
    run_once(client.session_factory, get_settings(), store)
    listing = client.get(url, headers=auth).json()
    assert listing['active_revision_id'] == new
    assert all(r['state'] == 'ready' for r in listing['revisions'])
