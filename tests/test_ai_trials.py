from uuid import uuid4

from sqlalchemy import select

from control_plane.models import AIProjectDraft, AIProjectTrial, AITrialSession, AuditEvent
from control_plane.settings import get_settings
from test_ai_projects import setup, OWNER, PREFIX
from test_project_versions import versions


def initialized(versions, monkeypatch):
    client, auth, _ = setup(versions, monkeypatch)
    monkeypatch.setenv('PLATFORM_AI_TRIALS_ENABLED', 'true')
    get_settings.cache_clear()
    created = client.post(PREFIX, headers=auth, json={'request_id': str(uuid4()), 'name': '助手',
        'purpose': '售后', 'native_owner_id': OWNER}).json()
    path = PREFIX+'/'+created['project']['id']
    assert client.post(path+'/confirm', headers=auth, json={'native_owner_id': OWNER, 'agent_id': created['agent_id']}).status_code == 200
    config = {'instructions': '仅按资料回答。', 'model': 'test-model', 'temperature': 0,
              'max_output_tokens': 2048, 'response_style': 'grounded'}
    initial = client.post(path+'/draft/initialize', headers=auth, json={'native_owner_id': OWNER, 'config': config})
    assert initial.status_code == 200, initial.text
    return client, auth, path, config, initial.json()


def test_fixed_snapshot_idempotence_cas_and_acceptance(versions, monkeypatch):
    client, auth, path, config, initial = initialized(versions, monkeypatch)
    owner = {'native_owner_id': OWNER}
    payload = {**owner, 'request_id': str(uuid4()), 'expected_digest': initial['digest']}
    first = client.post(path+'/trials', headers=auth, json=payload)
    assert first.status_code == 200, first.text
    trial = first.json()
    assert client.post(path+'/trials', headers=auth, json=payload).json() == trial
    trial_path = path+'/trials/'+trial['id']
    assert client.post(trial_path+'/prepare', headers=auth, json=owner).json()['initial_agent']['instructions'] == config['instructions']
    assert client.post(trial_path+'/confirm', headers=auth, json=owner).json()['state'] == 'ready'
    session = {**owner, 'conversation_id': str(uuid4()), 'message_id': str(uuid4())}
    runtime = client.post(trial_path+'/resolve', headers=auth, json=session).json()
    receipt = {**session, 'outcome': 'passed', 'note': '已核对回答和来源',
        'transcript_digest': 'a'*64, 'answer_checked': True, 'sources_checked': True}
    accepted = client.post(trial_path+'/acceptance', headers=auth, json=receipt)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['acceptance_valid']
    assert client.post(trial_path+'/acceptance', headers=auth, json=receipt).json() == accepted.json()
    assert client.post(trial_path+'/acceptance', headers=auth, json={**receipt, 'note': 'overwrite'}).status_code == 409
    changed = client.post(path+'/draft', headers=auth, json={**owner, 'expected_digest': initial['digest'],
        'config': {**config, 'instructions': '新指令', 'response_style': 'support'}})
    assert changed.status_code == 200, changed.text
    assert not changed.json()['trials'][0]['acceptance_valid']
    assert client.post(path+'/draft', headers=auth, json={**owner, 'expected_digest': initial['digest'], 'config': config}).status_code == 409
    after = client.post(trial_path+'/resolve', headers=auth, json=session).json()
    assert after['snapshot'] == runtime['snapshot']
    assert client.post(path+'/trials', headers=auth, json={**payload, 'request_id': str(uuid4())}).status_code == 409
    newer = client.post(path+'/trials', headers=auth, json={**payload, 'request_id': str(uuid4()), 'expected_digest': changed.json()['digest']}).json()
    newer_path = path+'/trials/'+newer['id']
    client.post(newer_path+'/confirm', headers=auth, json=owner)
    assert client.post(newer_path+'/resolve', headers=auth, json=session).status_code == 409
    assert client.post(newer_path+'/acceptance', headers=auth, json=receipt).status_code == 409
    with client.session_factory() as db:
        assert len(list(db.scalars(select(AIProjectDraft)))) == 1
        assert len(list(db.scalars(select(AIProjectTrial)))) == 2
        assert len(list(db.scalars(select(AITrialSession)))) == 1
        assert len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == 'ai_trial.accepted_passed')))) == 1


def test_scope_policy_and_input_rejection(versions, monkeypatch):
    client, auth, path, config, initial = initialized(versions, monkeypatch)
    payload = {'native_owner_id': OWNER, 'request_id': str(uuid4()), 'expected_digest': initial['digest']}
    assert client.post(path+'/trials', headers=auth, json={**payload, 'snapshot': {}}).status_code == 422
    assert client.post(path+'/trials', headers=auth, json={**payload, 'native_owner_id': 'b'*24}).status_code == 403
    assert client.get(path+'/trials', headers={**auth, 'X-CodeChat-Subject': 'foreign'}).status_code == 404
    trial = client.post(path+'/trials', headers=auth, json=payload).json()
    target = path+'/trials/'+trial['id']
    session = {'native_owner_id': OWNER, 'conversation_id': str(uuid4()), 'message_id': str(uuid4())}
    assert client.post(target+'/resolve', headers=auth, json=session).status_code == 409
    assert client.post(target+'/prepare', headers=auth, json={'native_owner_id': OWNER}).status_code == 200
    monkeypatch.setenv('PLATFORM_LITELLM_MODELS', 'other-model')
    get_settings.cache_clear()
    assert client.post(target+'/confirm', headers=auth, json={'native_owner_id': OWNER}).status_code == 409
    monkeypatch.setenv('PLATFORM_AI_TRIALS_ENABLED', 'false')
    get_settings.cache_clear()
    assert client.get(path+'/trials', headers=auth).status_code == 404


def test_knowledge_changes_invalidate_confirmation_but_old_trial_keeps_revision(versions, monkeypatch):
    from control_plane.models import AIProjectBinding, ProjectKnowledge
    client, auth, path, _, _ = initialized(versions, monkeypatch)
    monkeypatch.setenv('PLATFORM_KNOWLEDGE_ENABLED', 'true')
    get_settings.cache_clear()
    project = path.rsplit('/', 1)[1]
    ids = [str(uuid4()), str(uuid4())]
    with client.session_factory.begin() as db:
        binding = db.get(AIProjectBinding, project)
        for identifier in ids:
            db.add(ProjectKnowledge(id=identifier, project_id=project, environment_id=binding.environment_id,
                request_id=str(uuid4()), digest=('a' if identifier == ids[0] else 'b')*64, encrypted_source='test-encrypted',
                manifest=[], source_bytes=10, index_bytes=10, chunks=1, state='ready'))
        binding.knowledge_revision_id = ids[0]
    initial = client.get(path+'/trials', headers=auth).json()
    owner = {'native_owner_id': OWNER}
    row = client.post(path+'/trials', headers=auth, json={**owner, 'request_id':str(uuid4()), 'expected_digest':initial['digest']}).json()
    target = path+'/trials/'+row['id']
    client.post(target+'/confirm', headers=auth, json=owner)
    session = {**owner, 'conversation_id':str(uuid4()), 'message_id':str(uuid4())}
    assert client.post(target+'/resolve', headers=auth, json=session).json()['snapshot']['knowledge'][0]['index_revision'] == ids[0]
    with client.session_factory.begin() as db:
        db.get(AIProjectBinding, project).knowledge_revision_id = ids[1]
    assert client.get(path+'/trials', headers=auth).json()['digest'] != initial['digest']
    assert client.post(target+'/resolve', headers=auth, json=session).json()['knowledge_revision_id'] == ids[0]
    receipt = {**session, 'outcome':'passed', 'note':'checked', 'transcript_digest':'a'*64, 'answer_checked':True, 'sources_checked':True}
    assert client.post(target+'/acceptance', headers=auth, json=receipt).status_code == 409
    with client.session_factory.begin() as db:
        db.get(ProjectKnowledge, ids[0]).state = 'deleting'
    response = client.post(target+'/resolve', headers=auth, json=session)
    assert response.status_code == 409 and response.json()['detail'] == 'ai_trials_knowledge_unavailable'
