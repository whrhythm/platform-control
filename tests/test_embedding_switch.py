"""Exercise profile selection, real pgvector lookup, and fixed release routing together."""
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from control_plane import knowledge_store, main
from control_plane.knowledge import KnowledgeClient
from control_plane.models import EmbeddingPreference, EmbeddingProfile
from control_plane.settings import get_settings
from test_ai_projects import OWNER
from test_ai_releases import launched
from test_ai_trials import initialized
from test_knowledge_libraries import URL, change, create
from test_project_versions import versions


def test_public_transport_rejects_private_dns_before_sending_credentials(monkeypatch):
    import asyncio

    async def private(*args, **kwargs):
        return [(None, None, None, None, ('127.0.0.1', 443))]

    monkeypatch.setattr(asyncio.BaseEventLoop, 'getaddrinfo', private)
    profile = knowledge_store.ExternalEmbedding(id=uuid4(), url='https://embedding.example/v1/embeddings',
        model='external', secret=SecretStr('test-token'))
    with pytest.raises(ValueError, match='address_not_public'):
        asyncio.run(knowledge_store.external_embeddings(profile, ['test'], 3))


@pytest.fixture
def vector_store(monkeypatch):
    dsn = os.getenv('EMBEDDING_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('requires a disposable PostgreSQL with pgvector')
    schema = 'embedding_test_' + uuid4().hex
    sql = psycopg.sql
    with psycopg.connect(dsn) as db:
        db.execute('CREATE EXTENSION IF NOT EXISTS vector')
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        connect = lambda: psycopg.connect(dsn, options=f'-c search_path={schema},public')
        with connect() as db:
            db.execute('CREATE TABLE knowledge_revisions(scope uuid NOT NULL, revision uuid NOT NULL, '
                       'digest text NOT NULL, deleted boolean NOT NULL, source_bytes bigint NOT NULL, '
                       'index_bytes bigint NOT NULL, chunk_count integer NOT NULL, PRIMARY KEY(scope, revision))')
            db.execute('CREATE TABLE knowledge_chunks(scope uuid NOT NULL, revision uuid NOT NULL, ordinal integer NOT NULL, '
                       'filename text NOT NULL, line integer NOT NULL, content text NOT NULL, embedding vector(512) NOT NULL, '
                       'PRIMARY KEY(scope, revision, ordinal))')
        secret = 's' * 40
        legacy = lambda texts: [[1.0, 0.0] + [0.0]*510 for _ in texts]
        def transport(method, path, payload=None, **kwargs):
            response = service.request(method, path, json=payload, headers={'Authorization': 'Bearer '+secret})
            response.raise_for_status()
            return response.json()

        with connect() as db:
            db.execute('ALTER TABLE knowledge_revisions ADD COLUMN embedding_profile_id uuid')
        with TestClient(knowledge_store.create_app(connect, legacy, secret)) as service:
            monkeypatch.setattr(KnowledgeClient, '__init__', lambda self, settings: None)
            monkeypatch.setattr(KnowledgeClient, 'request', lambda self, *args, **kwargs: transport(*args, **kwargs))
            yield service, secret
    finally:
        with psycopg.connect(dsn) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_config_rebuild_search_and_published_local_revision(versions, vector_store, monkeypatch):
    from types import SimpleNamespace
    from control_plane.knowledge_worker import run_once

    monkeypatch.setenv('PLATFORM_KNOWLEDGE_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_AI_RELEASES_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_LITELLM_URL', 'http://model.test')
    monkeypatch.setattr('control_plane.knowledge.call_files', versions[4])
    get_settings.cache_clear()
    client, auth, project, _, _ = initialized(versions, monkeypatch)
    admin_path = '/v1/admin/knowledge-embedding'
    assert client.get(admin_path).status_code == 401
    monkeypatch.setitem(main.app.dependency_overrides, main.current_principal,
                        lambda: SimpleNamespace(is_platform_admin=False))
    assert client.get(admin_path).status_code == 403
    monkeypatch.setitem(main.app.dependency_overrides, main.current_principal,
                        lambda: SimpleNamespace(is_platform_admin=True, issuer='test', subject='admin'))
    with client.session_factory.begin() as db:
        db.add(EmbeddingPreference(id=1, version=0))

    calls = []
    broken = False

    def provider(request):
        body = json.loads(request.content)
        assert body['model'] == 'external-512' and body['dimensions'] == 512
        assert request.url.path == '/v1/embeddings'
        calls.extend(body['input'])
        rows = [{'index': index, 'embedding': [0.0, 1.0]+[0.0]*510 if not broken else [1.0]}
                for index, _ in enumerate(body['input'])]
        return httpx.Response(200, json={'data': rows[::-1]})

    monkeypatch.setattr(knowledge_store, 'PublicTransport', lambda *args, **kwargs: httpx.MockTransport(provider))
    assert client.get(admin_path).json()['selected'] == 'local'
    library = create(client, auth)
    library = change(client, auth, library, action='upsert',
                     files=[{'name': 'policy.md', 'content': '合成政策，保留旧版。'}])
    old = library['head_revision_id']
    run_once(client.session_factory, get_settings())
    owner = {'native_owner_id': OWNER}
    assert client.post(project+'/knowledge-library', headers=auth, json={
        **owner, 'library_id': library['id'], 'expected_library_id': None}).status_code == 200
    digest = client.get(project+'/trials', headers=auth).json()['digest']
    trial = client.post(project+'/trials', headers=auth, json={
        **owner, 'request_id': str(uuid4()), 'expected_digest': digest}).json()
    trial_path = project+'/trials/'+trial['id']
    assert trial['knowledge_revision_id'] == old
    client.post(trial_path+'/confirm', headers=auth, json=owner).raise_for_status()
    run = {**owner, 'conversation_id': str(uuid4()), 'message_id': str(uuid4())}
    client.post(trial_path+'/resolve', headers=auth, json=run).raise_for_status()
    client.post(trial_path+'/acceptance', headers=auth, json={**run, 'outcome': 'passed', 'note': 'verified',
        'transcript_digest': 'a'*64, 'answer_checked': True, 'sources_checked': True}).raise_for_status()
    client.post(project+'/releases', headers=auth, json={
        **owner, 'request_id': str(uuid4()), 'trial_id': trial['id'], 'expected_digest': digest,
        'budget_micros': 100000, 'confirm_sponsor': True}).raise_for_status()
    _, entry, _, _ = launched(client, auth, project)

    profile = {'url': 'https://embedding.example/v1/embeddings', 'model': 'external-512',
               'secret': 'test-provider-key', 'request_dimensions': True}
    broken = True
    assert client.post(admin_path+'/profiles', json=profile).status_code == 503
    assert len(client.get(admin_path).json()['profiles']) == 1
    broken = False
    added = client.post(admin_path+'/profiles', json=profile)
    assert added.status_code == 201 and profile['secret'] not in added.text
    identifier = added.json()['profiles'][1]['id']
    with client.session_factory() as db:
        assert profile['secret'] not in db.get(EmbeddingProfile, identifier).encrypted_secret
    assert client.put(admin_path, json={'profile': identifier, 'version': 0}).json()['selected'] == identifier
    assert client.put(admin_path, json={'profile': None, 'version': 0}).status_code == 409
    assert calls == ['Embedding connection check']*2

    path = URL+'/'+library['id']
    rebuild = {'request_id': str(uuid4()), 'expected_revision_id': old, 'action': 'rebuild',
               'embedding_profile': identifier}
    assert client.post(path+'/files', headers={**auth, 'X-CodeChat-Subject': 'foreign'}, json=rebuild).status_code == 404
    pending = change(client, auth, library, action='rebuild', embedding_profile=identifier)
    new = pending['head_revision_id']
    assert new != old and pending['active_revision_id'] == old
    broken = True
    run_once(client.session_factory, get_settings())
    failed = client.get(path, headers=auth).json()
    assert failed['active_revision_id'] == old and failed['head']['state'] == 'failed'
    client.post(path+'/retry', headers=auth).raise_for_status()
    broken = False
    run_once(client.session_factory, get_settings())
    assert client.get(path, headers=auth).json()['active_revision_id'] == new
    draft = client.post(project+'/knowledge/search', headers=auth, json={**owner, 'query': '新检索'})
    assert draft.json()['revision_id'] == new and draft.json()['sources'][0]['name'] == 'policy.md'
    assert calls[-1] == '新检索'
    native = client.post(path+'/search', headers=auth, json={'query': '原生智能体检索'})
    assert native.json()['revision_id'] == new and native.json()['sources'][0]['name'] == 'policy.md'
    assert client.post(path+'/search', headers={**auth, 'X-CodeChat-Subject': 'foreign'},
                       json={'query': 'foreign'}).status_code == 404
    assert calls[-1] == '原生智能体检索'
    count = len(calls)
    published = client.post(entry+'/search', headers=auth, json={**owner, 'query': '旧检索'})
    assert published.json()['revision_id'] == old and published.json()['sources'][0]['name'] == 'policy.md'
    assert len(calls) == count
    client.put(admin_path, json={'profile': None, 'version': 1}).raise_for_status()
    assert client.get(path, headers=auth).json()['active_revision_id'] == new
    assert client.post(path+'/cleanup', headers=auth, json={'revision_id': old}).status_code == 409
