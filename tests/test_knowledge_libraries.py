from uuid import uuid4

from control_plane.knowledge_worker import run_once
from control_plane.models import AIProjectBinding, ProjectKnowledge, KnowledgeLibrary
from control_plane.settings import get_settings
from test_knowledge import initialized, Store, OWNER, PREFIX
from test_project_versions import versions

URL = '/internal/v1/workspace/knowledge-libraries'


def create(client, auth, name='资料库'):
    body = {'request_id': str(uuid4()), 'name': name}
    result = client.post(URL, headers=auth, json=body)
    assert result.status_code == 201, result.text
    assert client.post(URL, headers=auth, json=body).json()['id'] == result.json()['id']
    return result.json()


def change(client, auth, library, **values):
    body = {'request_id': str(uuid4()), 'expected_revision_id': library['head_revision_id'], **values}
    result = client.post(URL+'/'+library['id']+'/files', headers=auth, json=body)
    assert result.status_code == 202, result.text
    assert client.post(URL+'/'+library['id']+'/files', headers=auth, json=body).json()['head_revision_id'] == result.json()['head_revision_id']
    return result.json()


def test_multilibrary_file_lifecycle_binding_isolation_and_cleanup(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    library = create(client, auth)
    other = create(client, auth, '第二个资料库')
    assert len(client.get(URL, headers=auth).json()['libraries']) == 2
    path = URL+'/'+library['id']
    assert client.get(path, headers={**auth, 'X-CodeChat-Subject': 'foreign'}).status_code == 404
    bind = PREFIX+'/'+project+'/knowledge-library'
    body = {'library_id': library['id'], 'expected_library_id': None, 'native_owner_id': OWNER}
    assert client.post(bind, headers=auth, json={**body, 'native_owner_id':'b'*24}).status_code == 403
    assert client.post(bind, headers=auth, json=body).status_code == 200
    assert client.post(path+'/remove', headers=auth).json()['detail'] == 'knowledge_library_in_use'
    library = change(client, auth, library, action='upsert', files=[{'name':'规则.md','content':'换货七天。'}])
    first = library['head_revision_id']
    assert library['head']['state'] == 'queued'
    source = client.get(path+'/source', headers=auth, params={'revision_id':first,'name':'规则.md'})
    assert source.json()['content'] == '换货七天。'  # Can edit original even when indexing is down.
    assert client.get(URL+'/'+other['id']+'/source', headers=auth, params={'revision_id':first,'name':'规则.md'}).status_code == 404
    store = Store()
    run_once(client.session_factory, get_settings(), store)
    with client.session_factory() as db:
        assert db.get(AIProjectBinding, project).knowledge_revision_id == first
        assert '换货' not in db.get(ProjectKnowledge, first).encrypted_source
    stale = client.post(path+'/files', headers=auth, json={'request_id':str(uuid4()), 'expected_revision_id':None,
        'action':'remove', 'name':'规则.md'})
    assert stale.status_code == 409
    library = change(client, auth, library, action='upsert', files=[{'name':'规则.md','content':'换货十四天。'}])
    second = library['head_revision_id']
    store.fail = True
    run_once(client.session_factory, get_settings(), store)
    failed = client.get(path, headers=auth).json()
    assert failed['head']['state'] == 'failed' and failed['active_revision_id'] == first
    store.fail = False
    assert client.post(path+'/retry', headers=auth).status_code == 200
    run_once(client.session_factory, get_settings(), store)
    library = change(client, auth, library, action='rename', name='规则.md', new_name='新规则.md')
    run_once(client.session_factory, get_settings(), store)
    assert library['head']['manifest'][0]['name'] == '新规则.md'
    library = change(client, auth, library, action='remove', name='新规则.md')
    puts = store.puts
    run_once(client.session_factory, get_settings(), store)
    assert store.puts == puts  # Empty revision never contacts embedding service.
    empty = client.get(path, headers=auth).json()
    assert empty['head']['state'] == 'ready' and empty['head']['manifest'] == []
    assert client.post(PREFIX+'/'+project+'/knowledge/search', headers=auth, json={'native_owner_id':OWNER,'query':'期限'}).json()['sources'] == []
    assert client.post(path+'/cleanup', headers=auth, json={'revision_id':empty['head_revision_id']}).status_code == 409
    assert client.post(path+'/cleanup', headers=auth, json={'revision_id':first}).status_code == 200
    run_once(client.session_factory, get_settings(), store)
    with client.session_factory() as db:
        assert db.get(ProjectKnowledge, first).state == 'deleted'
    assert client.post(bind, headers=auth, json={'library_id':None,'expected_library_id':library['id'],'native_owner_id':OWNER}).status_code == 200
    assert client.post(path+'/remove', headers=auth).status_code == 200
    for _ in range(5): run_once(client.session_factory, get_settings(), store)
    with client.session_factory() as db:
        assert db.get(KnowledgeLibrary, library['id']).state == 'deleted'


def test_late_index_cannot_replace_latest_revision_and_two_projects_share(versions, monkeypatch):
    client, auth, project = initialized(versions, monkeypatch)
    library = create(client, auth)
    other = client.post(PREFIX, headers=auth, json={'request_id':str(uuid4()),'name':'另一个助手','purpose':'问答','native_owner_id':OWNER}).json()
    client.post(PREFIX+'/'+other['project']['id']+'/confirm', headers=auth,json={'native_owner_id':OWNER,'agent_id':other['agent_id']})
    for p in (project, other['project']['id']):
        assert client.post(PREFIX+'/'+p+'/knowledge-library', headers=auth,json={
            'library_id':library['id'],'expected_library_id':None,'native_owner_id':OWNER}).status_code == 200
    old = change(client, auth, library, action='upsert', files=[{'name':'a.txt','content':'旧'}])
    new = change(client, auth, old, action='upsert', files=[{'name':'a.txt','content':'新'}])
    store = Store()
    run_once(client.session_factory, get_settings(), store)
    assert client.get(URL+'/'+library['id'], headers=auth).json()['active_revision_id'] is None
    run_once(client.session_factory, get_settings(), store)
    with client.session_factory() as db:
        for p in (project,other['project']['id']):
            assert db.get(AIProjectBinding,p).knowledge_revision_id == new['head_revision_id']
    assert len(client.get(URL+'/'+library['id'],headers=auth).json()['references']['projects']) == 2
    # Legacy import cannot silently replace a shared library's whole file set.
    response=client.post(PREFIX+'/'+project+'/knowledge',headers=auth,json={
        'request_id':str(uuid4()),'native_owner_id':OWNER,'files':[{'name':'x.txt','content':'replacement'}]})
    assert response.status_code == 409


def test_library_update_preserves_fixed_trial_and_prevents_cleanup(versions, monkeypatch):
    from test_ai_trials import initialized as trial_project
    client, auth, path, _, _ = trial_project(versions, monkeypatch)
    monkeypatch.setenv('PLATFORM_KNOWLEDGE_ENABLED', 'true')
    monkeypatch.setattr('control_plane.knowledge.call_files', versions[4])
    get_settings.cache_clear()
    library = create(client, auth)
    owner = {'native_owner_id': OWNER}
    assert client.post(path+'/knowledge-library', headers=auth, json={**owner,
        'library_id':library['id'], 'expected_library_id':None}).status_code == 200
    first = change(client, auth, library, action='upsert', files=[{'name':'policy.md','content':'七天'}])
    run_once(client.session_factory, get_settings(), Store())
    initial = client.get(path+'/trials', headers=auth).json()
    trial = client.post(path+'/trials', headers=auth, json={**owner,
        'request_id':str(uuid4()), 'expected_digest':initial['digest']})
    assert trial.status_code == 200, trial.text
    target = path+'/trials/'+trial.json()['id']
    assert client.post(target+'/confirm', headers=auth, json=owner).status_code == 200
    session = {**owner, 'conversation_id':str(uuid4()), 'message_id':str(uuid4())}
    before = client.post(target+'/resolve', headers=auth, json=session)
    assert before.status_code == 200, before.text
    assert before.json()['snapshot']['knowledge'][0]['collection_id'] == library['id']
    change(client, auth, first, action='upsert', files=[{'name':'policy.md','content':'十四天'}])
    run_once(client.session_factory, get_settings(), Store())
    assert client.post(target+'/resolve', headers=auth, json=session).json()['snapshot'] == before.json()['snapshot']
    assert client.get(path+'/trials', headers=auth).json()['digest'] != initial['digest']
    old = client.get(path+'/knowledge/'+first['head_revision_id']+'/source', headers=auth,
        params={'name':'policy.md'})
    assert old.status_code == 200 and old.json()['content'] == '七天'
    assert client.post(URL+'/'+library['id']+'/cleanup', headers=auth,
        json={'revision_id':first['head_revision_id']}).status_code == 409
    assert client.post(path+'/knowledge-library', headers=auth, json={**owner,
        'library_id':None, 'expected_library_id':library['id']}).status_code == 200
    assert client.post(target+'/resolve', headers=auth, json=session).status_code == 200
    assert client.post(URL+'/'+library['id']+'/remove', headers=auth).status_code == 409


def test_edit_idempotency_conflicts_and_limits(versions, monkeypatch):
    client, auth, _ = initialized(versions, monkeypatch)
    library = create(client, auth)
    path = URL+'/'+library['id']
    body = {'request_id':str(uuid4()), 'expected_revision_id':None,
        'action':'upsert', 'files':[{'name':'one.txt','content':'first'}]}
    first = client.post(path+'/files',headers=auth,json=body)
    assert first.status_code == 202
    changed = {**body, 'files':[{'name':'one.txt','content':'different'}]}
    assert client.post(path+'/files',headers=auth,json=changed).status_code == 409
    monkeypatch.setenv('PLATFORM_KNOWLEDGE_REVISION_LIMIT','1')
    get_settings.cache_clear()
    response = client.post(path+'/files',headers=auth,json={**body, 'request_id':str(uuid4()),
        'expected_revision_id':first.json()['head_revision_id']})
    assert response.status_code == 409 and response.json()['detail'] == 'knowledge_revision_limit'
    assert client.get(path,headers=auth).json()['head_revision_id'] == first.json()['head_revision_id']


def test_real_http_index_commit_reply_loss_and_cleanup_recovery(versions, monkeypatch):
    import json
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from control_plane.knowledge import KnowledgeClient, knowledge_usage
    from control_plane.knowledge_format import prepare
    rows, requests = {}, []
    failures = {'put_reply':True, 'delete':True}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, status, data):
            encoded = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Length',str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        def do_GET(self):
            requests.append('GET')
            self.reply(200 if self.path in rows else 404, rows.get(self.path, {}))
        def do_PUT(self):
            requests.append('PUT')
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            digest, size, index, chunks = prepare(payload['files'])
            rows[self.path] = dict(digest=digest,source_bytes=size,index_bytes=index,chunks=len(chunks))
            if failures['put_reply']:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.reply(200, rows[self.path])
        def do_DELETE(self):
            requests.append('DELETE')
            if failures['delete']: return self.reply(503, {})
            rows.pop(self.path,None)
            self.reply(200, {'deleted':True})
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        client, auth, _ = initialized(versions, monkeypatch)
        monkeypatch.setenv('PLATFORM_KNOWLEDGE_URL',f'http://127.0.0.1:{server.server_port}')
        monkeypatch.setenv('PLATFORM_KNOWLEDGE_SERVICE_TOKEN','t'*40)
        get_settings.cache_clear()
        library = change(client,auth,create(client,auth),action='upsert',files=[{'name':'a.md','content':'合成资料'}])
        path = URL+'/'+library['id']
        provider = KnowledgeClient(get_settings())
        run_once(client.session_factory,get_settings(),provider)
        assert client.get(path,headers=auth).json()['head']['state'] == 'failed'
        assert client.post(path+'/retry',headers=auth).status_code == 200
        run_once(client.session_factory,get_settings(),provider)
        assert client.get(path,headers=auth).json()['head']['state'] == 'ready'
        assert requests.count('PUT') == 1  # Recovery verifies remote commit, never re-embeds.
        assert client.post(path+'/remove',headers=auth).status_code == 200
        assert client.post(path+'/remove',headers=auth).status_code == 200
        run_once(client.session_factory,get_settings(),provider)
        with client.session_factory.begin() as db:
            row = db.get(ProjectKnowledge,library['head_revision_id'])
            assert row.state == 'deleting' and knowledge_usage(db,row.environment_id) > 0
            row.lease_until = None
        failures['delete'] = False
        run_once(client.session_factory,get_settings(),provider)
        with client.session_factory() as db:
            row = db.get(ProjectKnowledge,library['head_revision_id'])
            assert row.state == 'deleted' and knowledge_usage(db,row.environment_id) == 0
            assert db.get(KnowledgeLibrary,library['id']).state == 'deleted'
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
