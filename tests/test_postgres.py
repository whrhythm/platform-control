import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy import select, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from control_plane.database import get_db, make_engine
from control_plane.main import app
from control_plane.models import Base, Tenant, UserEnvironment, WorkspaceVolumeBinding, WorkspaceProvisioningIntent, CapacityAccount
from control_plane.workspace_provisioning import observe_and_record_volume, provision_one, request_intent
from control_plane.settings import get_settings
from control_plane.worker import plan_one
from test_resources import request, setup_tenant


@pytest.fixture
def postgres_harness(harness, monkeypatch):
    url = os.environ.get("PLATFORM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Run scripts/test_postgres.py for a disposable PostgreSQL")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost"} and parsed.database.startswith("platform_test_"), "Refusing a non-local/non-test database"
    monkeypatch.setenv("PLATFORM_DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    engine = make_engine(url)
    sessions = sessionmaker(engine, expire_on_commit=False)

    def test_db():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = test_db
    client, token = harness
    client.session_factory = sessions
    yield client, token
    # First provisioning now atomically persists trial rights. Remove only this
    # disposable fixture's snapshots before exercising guarded schema downgrade.
    with sessions.begin() as db:
        db.execute(delete(CapacityAccount))
    engine.dispose()
    command.downgrade(config, "base")
    get_settings.cache_clear()


@pytest.mark.parametrize('limit_scope', ['application', 'member'])
def test_application_budget_concurrent_admission(postgres_harness, monkeypatch, limit_scope):
    from cryptography.fernet import Fernet
    from fastapi import HTTPException
    from control_plane.models import (AIApplication, AIRelease, AIAppMember, AIAppEntry, AIAppRun, AIAppCall,
        AIProjectTrial, AITrialSession, AITrialRun, AIProjectDraft, AIProjectBinding, WorkspaceProject,
        ModelCredential, AIConfiguration, StorageMeasurement, WorkspaceFileService)
    from control_plane.ai_sponsor import admission
    from test_capacity import prepare_capacity
    from test_ai_releases import sponsored
    client, _ = postgres_harness
    _, auth, env_id, *_ = prepare_capacity(client, monkeypatch)
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    get_settings.cache_clear()
    try:
        client, claims, payload, _ = sponsored((client,auth,env_id,None,None),monkeypatch)
        member_env_id = None
        if limit_scope == 'member':
            import jwt
            from test_ai_sharing import guest, invite, reply
            from test_ai_releases import launched
            from test_ai_projects import PREFIX
            with client.session_factory() as db:
                project_id = db.scalar(select(AIApplication)).project_id
            path = PREFIX+'/'+project_id
            member_auth, member_env_id = guest(client,auth,env_id)
            assert invite(client,auth,path).status_code == 200
            assert reply(client,member_auth,path).status_code == 200
            *_, runtime = launched(client,member_auth,path,'b'*24)
            claims = jwt.decode(runtime['credential'][6:],get_settings().librechat_service_token,
                algorithms=['HS256'],audience='ai-application')
        with client.session_factory() as db:
            identifier, _, _, _ = admission(db,claims,payload,get_settings())
            call=db.get(AIAppCall,identifier)
            if limit_scope == 'member':
                db.get(AIAppMember,(call.project_id,member_env_id)).budget_micros=call.reserved_micros+100
            else:
                db.get(AIApplication,call.project_id).budget_micros=call.reserved_micros+100
            db.delete(call)
            db.commit()
        def attempt(i):
            with client.session_factory() as db:
                try:
                    admission(db,claims,{**payload,'messages':[{'role':'user','content':'q'+str(i)}]},get_settings())
                    return 200
                except HTTPException as error:
                    return error.status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(attempt,range(8)))
        assert results.count(200)==1 and results.count(429)==7
        with client.session_factory() as db:
            calls=list(db.scalars(select(AIAppCall)))
            assert len(calls)==1
            assert calls[0].held_micros<=db.get(AIApplication,calls[0].project_id).budget_micros
    finally:
        with client.session_factory.begin() as db:
            for model in (AIAppCall,AIAppRun,AIAppEntry,AIAppMember,AIRelease,AIApplication,AITrialRun,
                          AITrialSession,AIProjectTrial,AIProjectDraft,AIProjectBinding,WorkspaceProject,
                          ModelCredential,AIConfiguration,StorageMeasurement,CapacityAccount,WorkspaceFileService,
                          WorkspaceVolumeBinding,WorkspaceProvisioningIntent):
                db.execute(delete(model))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id==env_id))
            if member_env_id:
                db.execute(delete(UserEnvironment).where(UserEnvironment.id==member_env_id))


def test_parallel_requests_never_exceed_tenant_capacity(postgres_harness):
    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)

    def create(index):
        return request(client, path, project, auth, {"kind": "workspace", "name": f"dev-{index}"}, f"parallel-request-{index}").status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(create, range(20)))
    assert statuses.count(201) == 8
    assert statuses.count(409) == 12
    reserved = client.get(path + "/quota", headers=auth).json()["reserved"]
    assert reserved["cpu_millicores"] == 4000
    assert reserved["memory_mib"] == 8192
    assert reserved["storage_gib"] == 40
    assert len(client.get(path + "/operations", headers=auth).json()) == 8


def test_ai_project_concurrent_retry_and_project_limit(postgres_harness, monkeypatch):
    from uuid import uuid4
    from cryptography.fernet import Fernet
    from control_plane.models import AIProjectBinding, WorkspaceProject, StorageMeasurement, WorkspaceFileService
    from test_capacity import prepare_capacity
    client, _ = postgres_harness
    client, auth, env_id, _, _ = prepare_capacity(client, monkeypatch)
    monkeypatch.setenv('PLATFORM_AI_PROJECTS_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_WORKSPACE_PROJECTS_LIMIT', '3')
    monkeypatch.setenv('PLATFORM_LITELLM_MODELS', 'test-model')
    monkeypatch.setenv('PLATFORM_CAPABILITY_CHAT_MODEL', 'test-model')
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    get_settings.cache_clear()
    payload = {'request_id': str(uuid4()), 'name': 'Concurrent', 'purpose': 'Test', 'native_owner_id': 'a'*24}
    url = '/internal/v1/workspace/ai-projects'
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: client.post(url, headers=auth, json=payload), range(8)))
        assert all(reply.status_code == 201 for reply in replies)
        assert len({reply.json()['agent_id'] for reply in replies}) == 1
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: client.post(url, headers=auth,
                json={**payload, 'request_id': str(uuid4())}), range(8)))
        assert [reply.status_code for reply in replies].count(201) == 2
        assert [reply.status_code for reply in replies].count(409) == 6
        assert len(client.get(url, headers=auth).json()['projects']) == 3
    finally:
        with client.session_factory.begin() as db:
            for model in (AIProjectBinding, WorkspaceProject, StorageMeasurement, CapacityAccount,
                          WorkspaceFileService, WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id == env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))


def test_trials_concurrent_retry_and_draft_compare_and_swap(postgres_harness, monkeypatch):
    from uuid import uuid4
    from cryptography.fernet import Fernet
    from control_plane.models import AIProjectBinding, AIProjectDraft, AIProjectTrial, AITrialSession, AITrialRun, WorkspaceProject, StorageMeasurement, WorkspaceFileService
    from test_capacity import prepare_capacity
    client, _ = postgres_harness
    client, auth, env_id, _, _ = prepare_capacity(client, monkeypatch)
    for key, value in {'PLATFORM_AI_PROJECTS_ENABLED':'true', 'PLATFORM_AI_TRIALS_ENABLED':'true',
        'PLATFORM_AI_TRIALS_LIMIT':'2', 'PLATFORM_LITELLM_MODELS':'test-model', 'PLATFORM_CAPABILITY_CHAT_MODEL':'test-model',
        'PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY':Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    owner = {'native_owner_id':'a'*24}
    base = '/internal/v1/workspace/ai-projects'
    created = client.post(base, headers=auth, json={**owner, 'request_id':str(uuid4()), 'name':'Concurrent trial', 'purpose':'Test'}).json()
    identifier = created['project']['id']
    path = base+'/'+identifier
    try:
        client.post(path+'/confirm', headers=auth, json={**owner, 'agent_id':created['agent_id']})
        config = {'instructions':'original', 'model':'test-model', 'temperature':0, 'max_output_tokens':512, 'response_style':'grounded'}
        initial = client.post(path+'/draft/initialize', headers=auth, json={**owner, 'config':config}).json()
        body = {**owner, 'request_id':str(uuid4()), 'expected_digest':initial['digest']}
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _:client.post(path+'/trials', headers=auth, json=body), range(8)))
        assert all(r.status_code == 200 for r in responses)
        assert len({r.json()['id'] for r in responses}) == 1
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _:client.post(path+'/trials', headers=auth, json={**body, 'request_id':str(uuid4())}), range(8)))
        assert [r.status_code for r in responses].count(200) == 1
        assert [r.status_code for r in responses].count(409) == 7
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda n:client.post(path+'/draft', headers=auth, json={**owner,
                'expected_digest':initial['digest'], 'config':{**config, 'instructions':'changed '+str(n)}}), range(2)))
        assert sorted(r.status_code for r in responses) == [200, 409]
    finally:
        with client.session_factory.begin() as db:
            db.execute(delete(AITrialRun).where(AITrialRun.trial_id.in_(select(AIProjectTrial.id).where(AIProjectTrial.project_id == identifier))))
            db.execute(delete(AITrialSession).where(AITrialSession.environment_id == env_id))
            db.execute(delete(AIProjectTrial).where(AIProjectTrial.environment_id == env_id))
            db.execute(delete(AIProjectDraft).where(AIProjectDraft.project_id == identifier))
            for model in (AIProjectBinding, WorkspaceProject, StorageMeasurement, CapacityAccount,
                          WorkspaceFileService, WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id == env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))


def test_parallel_source_recovery_points_respect_user_limit(postgres_harness, monkeypatch):
    import httpx
    from cryptography.fernet import Fernet
    from test_capacity import prepare_capacity
    client, _ = postgres_harness
    client, auth, env_id, observed, _ = prepare_capacity(client, monkeypatch)
    monkeypatch.setenv('PLATFORM_PROJECT_VERSIONS_ENABLED','true')
    monkeypatch.setenv('PLATFORM_PROJECT_VERSIONS_LIMIT','1')
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY',Fernet.generate_key().decode())
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.project_versions.call_files',lambda *args,**kwargs:httpx.Response(200,
        json=observed if args[4]=='/v1/usage' else {'entries':{},'bytes':0,'excluded':[]}))
    def capture(index):
        return client.post('/internal/v1/workspace/versions',headers={**auth,'Idempotency-Key':f'parallel-source-{index:04d}'},
            json={'path':'project','name':'source'}).status_code
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses=list(pool.map(capture,range(6)))
        assert statuses.count(201)==1 and statuses.count(409)==5
    finally:
        from control_plane.models import ProjectVersion, ProjectRestore, StorageMeasurement, WorkspaceFileService
        with client.session_factory.begin() as db:
            for model in (ProjectRestore,ProjectVersion,StorageMeasurement,CapacityAccount,WorkspaceFileService,WorkspaceVolumeBinding,WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id==env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id==env_id))


def test_knowledge_parallel_imports_reserve_once_and_enforce_capacity(postgres_harness, monkeypatch):
    import httpx
    from uuid import uuid4
    from cryptography.fernet import Fernet
    from control_plane.models import AIProjectBinding, WorkspaceProject, ProjectKnowledge, KnowledgeLibrary, StorageMeasurement, WorkspaceFileService
    from control_plane.knowledge_format import prepare
    from test_capacity import prepare_capacity
    client, _ = postgres_harness
    client, auth, env_id, observed, _ = prepare_capacity(client, monkeypatch)
    for name, value in [('AI_PROJECTS_ENABLED','true'), ('KNOWLEDGE_ENABLED','true'),
        ('LITELLM_MODELS','test-model'), ('CAPABILITY_CHAT_MODEL','test-model'),
        ('MODEL_CREDENTIAL_ENCRYPTION_KEY',Fernet.generate_key().decode())]:
        monkeypatch.setenv('PLATFORM_'+name,value)
    get_settings.cache_clear()
    source=[{'name':'policy.md','content':'合成七天政策'}]
    _, size, index, _ = prepare(source)
    observed['used_bytes']=2*1024**3-size-index
    monkeypatch.setattr('control_plane.knowledge.call_files',lambda *a,**kw:httpx.Response(200,json=observed))
    url='/internal/v1/workspace/ai-projects'
    try:
        created=client.post(url,headers=auth,json={'request_id':str(uuid4()),'name':'知识并发','purpose':'测试','native_owner_id':'a'*24}).json()
        project=created['project']['id']
        client.post(url+'/'+project+'/confirm',headers=auth,json={'agent_id':created['agent_id'],'native_owner_id':'a'*24})
        path=url+'/'+project+'/knowledge'
        payload={'request_id':str(uuid4()),'files':source,'native_owner_id':'a'*24}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:client.post(path,headers=auth,json=payload),range(8)))
        assert all(r.status_code==202 for r in results), [r.text for r in results]
        assert len({r.json()['id'] for r in results})==1
        assert client.post(path,headers=auth,json={**payload,'request_id':str(uuid4())}).status_code==507
    finally:
        with client.session_factory.begin() as db:
            for model in (ProjectKnowledge,AIProjectBinding,KnowledgeLibrary,WorkspaceProject,StorageMeasurement,CapacityAccount,
                          WorkspaceFileService,WorkspaceVolumeBinding,WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id==env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id==env_id))


def test_library_backfill_preserves_postgres_releases(postgres_harness):
    from test_migrations import assert_library_backfill_preserves_pinned_data
    client, _ = postgres_harness
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    assert_library_backfill_preserves_pinned_data(config, client.session_factory.kw['bind'])


def test_library_parallel_edits_use_revision_cas(postgres_harness, monkeypatch):
    import httpx
    from uuid import uuid4
    from cryptography.fernet import Fernet
    from control_plane.models import KnowledgeLibrary, ProjectKnowledge, StorageMeasurement, WorkspaceFileService
    from test_capacity import prepare_capacity
    client, _ = postgres_harness
    client, auth, env_id, observed, _ = prepare_capacity(client, monkeypatch)
    for name, value in [('AI_PROJECTS_ENABLED','true'),('KNOWLEDGE_ENABLED','true'),
        ('MODEL_CREDENTIAL_ENCRYPTION_KEY',Fernet.generate_key().decode())]:
        monkeypatch.setenv('PLATFORM_'+name,value)
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.knowledge.call_files',lambda *a,**kw:httpx.Response(200,json=observed))
    url='/internal/v1/workspace/knowledge-libraries'
    try:
        library=client.post(url,headers=auth,json={'request_id':str(uuid4()),'name':'并发库'}).json()
        path=url+'/'+library['id']+'/files'
        payload={'request_id':str(uuid4()),'expected_revision_id':None,'action':'upsert','files':[{'name':'a.txt','content':'initial'}]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:client.post(path,headers=auth,json=payload),range(8)))
        assert all(r.status_code==202 for r in results), [r.text for r in results]
        assert len({r.json()['head_revision_id'] for r in results})==1
        revision=results[0].json()['head_revision_id']
        def edit(i):
            return client.post(path,headers=auth,json={**payload,'request_id':str(uuid4()),
                'expected_revision_id':revision,'files':[{'name':'a.txt','content':str(i)}]})
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(edit,range(4)))
        assert sorted(r.status_code for r in results)==[202,409,409,409]
    finally:
        with client.session_factory.begin() as db:
            for model in (ProjectKnowledge,KnowledgeLibrary,StorageMeasurement,CapacityAccount,
                          WorkspaceFileService,WorkspaceVolumeBinding,WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id==env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id==env_id))


def test_parallel_code_execution_reservations_are_bounded(postgres_harness, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy import delete
    from control_plane.auth import Principal
    from control_plane.code_execution import reserve, usage
    from control_plane.models import CodeExecution, CapacityAccount, CapacityOrder, StorageMeasurement, WorkspaceFileService
    from test_capacity import prepare_capacity
    client, _ = postgres_harness
    client, _auth, env_id, _observed, issuer = prepare_capacity(client, monkeypatch)
    def submit(index):
        with client.session_factory() as db:
            try:
                reserve(db, Principal(issuer, 'alice'), f'parallel-code-{index:04d}', '0'*64)
                return 201
            except HTTPException as error:
                return error.status_code
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(16)))
        assert results.count(201) == 1 and results.count(429) == 15
        with client.session_factory() as db:
            assert usage(db, db.get(UserEnvironment, env_id))['used_runs'] == 1
    finally:
        with client.session_factory.begin() as db:
            for model in (CodeExecution, StorageMeasurement, CapacityOrder, CapacityAccount, WorkspaceFileService, WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id == env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))


def test_parallel_agent_submissions_create_one_task(postgres_harness, monkeypatch):
    import httpx
    from cryptography.fernet import Fernet
    from sqlalchemy import delete
    from control_plane.agent_tasks import submit_task
    from control_plane.auth import Principal
    from control_plane.models import AgentTask, AgentPreference
    client, _ = postgres_harness
    for name, value in {'WORKSPACE_FILES_ENABLED': 'true', 'AGENT_TASKS_ENABLED': 'true',
                        'LIBRECHAT_SERVICE_TOKEN': 'x' * 40,
                        'MODEL_CREDENTIAL_ENCRYPTION_KEY': Fernet.generate_key().decode()}.items():
        monkeypatch.setenv('PLATFORM_' + name, value)
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.agent_tasks.call_files', lambda *a, **kw: httpx.Response(200, json={'entries': []}))
    auth = {'X-CodeChat-Service-Token': 'x' * 40, 'X-CodeChat-Subject': 'agent-owner'}
    assert client.post('/internal/v1/workspace/ensure', headers=auth).status_code == 200
    assert client.put('/internal/v1/workspace/agent/preferences', headers=auth, json={
        'conversation_id': 'default', 'revision': 0, 'project_path': 'demo',
        'strategy': 'implement', 'approval': 'strict'}).status_code == 200
    def submit(_):
        with client.session_factory() as db:
            return submit_task(db, Principal(get_settings().oidc_issuer, 'agent-owner'), get_settings(),
                               'thread', 'synthetic instruction', 'synthetic-model').id
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(submit, range(16)))
        assert len(set(ids)) == 1
        with client.session_factory() as db:
            assert len(list(db.scalars(select(AgentTask)))) == 1
    finally:
        with client.session_factory.begin() as db:
            db.execute(delete(AgentTask))
            db.execute(delete(AgentPreference))
            identifiers = select(UserEnvironment.id).where(UserEnvironment.subject == 'agent-owner')
            db.execute(delete(CapacityAccount).where(CapacityAccount.environment_id.in_(identifiers)))
            db.execute(delete(WorkspaceProvisioningIntent).where(WorkspaceProvisioningIntent.environment_id.in_(identifiers)))
            db.execute(delete(UserEnvironment).where(UserEnvironment.subject == 'agent-owner'))


def test_acceptance_concurrent_writers_preserve_one_revision(postgres_harness, monkeypatch):
    from test_agent_workbench import workspace, create
    from control_plane.models import AgentTask, AgentPreference, AuditEvent
    prepared = workspace.__wrapped__(postgres_harness, monkeypatch)
    client, headers, base = prepared
    task = create(prepared)
    with client.session_factory.begin() as db:
        db.get(AgentTask, task.id).status = 'completed'
    url = base + '/tasks/' + task.id + '/acceptance'
    value = {'revision': 0, 'goal': 'Synthetic concurrency test', 'criteria': [], 'note': ''}
    def write(index):
        return client.put(url, headers=headers, json={**value, 'note': str(index)}).status_code
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(write, range(6)))
        assert statuses.count(200) == 1 and statuses.count(409) == 5
        saved = client.get(url, headers=headers).json()
        assert saved['revision'] == 1
        from control_plane.release_provenance import freeze
        with client.session_factory() as db:
            frozen = freeze(db, task.environment_id, 'demo')
            assert frozen['candidate']['task_id'] == task.id
            assert frozen['candidate']['revision'] == 1
        retry = {key: saved[key] for key in ('goal', 'criteria', 'note')}
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(lambda _: client.put(url, headers=headers,
                json={**retry, 'revision': 0}).status_code, range(6)))
        assert statuses == [200] * 6
        with client.session_factory() as db:
            audit = list(db.scalars(select(AuditEvent).where(AuditEvent.action == 'agent.acceptance.saved')))
            assert len(audit) == 1
    finally:
        with client.session_factory.begin() as db:
            db.execute(delete(AgentTask))
            db.execute(delete(AgentPreference))
            identifiers = select(UserEnvironment.id).where(UserEnvironment.subject.in_(('alice', 'bob')))
            for model in (CapacityAccount, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id.in_(identifiers)))
            db.execute(delete(UserEnvironment).where(UserEnvironment.subject.in_(('alice', 'bob'))))


def test_parallel_idempotency_creates_one_resource(postgres_harness):
    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)

    def create(_):
        result = request(client, path, project, auth, {"kind": "workspace", "name": "dev"}, "shared-request-key")
        assert result.status_code == 201
        return result.json()["id"]
    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(pool.map(create, range(20)))
    assert len(set(ids)) == 1
    assert client.get(path + "/quota", headers=auth).json()["reserved"]["cpu_millicores"] == 500


def test_worker_and_cancellation_cannot_restore_cancelled_resource(postgres_harness):
    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)
    for index in range(10):
        resource = request(client, path, project, auth, {"kind": "workspace", "name": "dev"}, f"cancel-race-{index}").json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(plan_one, client.session_factory)
            cancellation = pool.submit(client.delete, path + "/resources/" + resource["id"], headers=auth)
            worker.result()
            assert cancellation.result().status_code == 200
        response = client.get(path + "/operations", headers=auth)
        assert response.status_code == 200, response.text
        operation = next(op for op in response.json() if op["resource_id"] == resource["id"])
        assert operation["status"] == "cancelled"
        assert operation["plan"] is None
        assert client.get(path + "/quota", headers=auth).json()["reserved"]["storage_gib"] == 0


def test_parallel_first_requests_create_one_environment(postgres_harness):
    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)
    url = f"{path}/projects/{project}/environment"

    def create(_):
        return client.post(url, headers=auth)

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(create, range(20)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    with client.session_factory() as db:
        accounts = list(db.scalars(select(CapacityAccount)))
        assert len(accounts) == 1 and accounts[0].plan['id'] == 'trial'


def test_volume_identifier_cannot_be_bound_to_two_users_concurrently(postgres_harness):
    client, _token = postgres_harness
    sessions = client.session_factory
    env_ids = []
    with sessions() as db:
        for user in ('alice-binding', 'bob-binding'):
            tenant = Tenant(name=user, owner_issuer='issuer', owner_subject=user, project_limit=1)
            db.add(tenant)
            db.flush()
            env = UserEnvironment(issuer='issuer', subject=user, identity_hash=user, storage_gib=2,
                                  first_tenant_id=tenant.id, first_project_id='project')
            db.add(env)
            db.flush()
            env_ids.append(env.id)
        db.commit()

    def insert(index):
        with sessions() as db:
            db.add(WorkspaceVolumeBinding(environment_id=env_ids[index], namespace_name=f'test-ns-{index}',
                                          namespace_uid='same-namespace-uid', pvc_uid=f'claim-{index}',
                                          pv_name=f'pv-{index}', pv_uid=f'pv-uid-{index}', volume_handle=f'volume-{index}'))
            try:
                db.commit()
                return 'created'
            except IntegrityError:
                db.rollback()
                return 'conflict'
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(insert, range(2))) == ['conflict', 'created']
    finally:
        with sessions() as db:
            for env_id in env_ids:
                binding = db.get(WorkspaceVolumeBinding, env_id)
                if binding is not None:
                    db.delete(binding)
            db.commit()


def test_parallel_provisioners_only_one_sends_create(postgres_harness):
    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)
    env_id = client.post(f'{path}/projects/{project}/environment', headers=auth).json()['id']
    sessions = client.session_factory
    request_intent(sessions, env_id)
    started, release = Event(), Event()

    class Provider:
        calls = 0

        def create_namespace(self, manifest):
            self.calls += 1
            started.set()
            assert release.wait(timeout=10)
            manifest['metadata']['uid'] = 'provider-created-ns'
            return manifest

    provider = Provider()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(provision_one, sessions, provider, env_id)
            try:
                assert started.wait(timeout=10)
                second = pool.submit(provision_one, sessions, provider, env_id)
                assert second.result(timeout=10) == 'namespace_inflight'
            finally:
                release.set()
            assert first.result(timeout=10) == 'namespace_recorded'
        assert provider.calls == 1
    finally:
        with sessions.begin() as db:
            intent = db.get(WorkspaceProvisioningIntent, env_id)
            if intent is not None:
                db.delete(intent)


def test_parallel_volume_observations_keep_one_binding(postgres_harness):
    from test_workspace_provisioning import FakeProvider

    client, token = postgres_harness
    auth, path, project = setup_tenant(client, token)
    env_id = client.post(f'{path}/projects/{project}/environment', headers=auth).json()['id']
    sessions = client.session_factory
    provider = FakeProvider()
    request_intent(sessions, env_id)
    for _ in range(4):
        provision_one(sessions, provider, env_id)
    provider.bind()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(lambda _: observe_and_record_volume(sessions, provider, env_id), range(2))) == ['bound', 'bound']
        with sessions() as db:
            binding = db.get(WorkspaceVolumeBinding, env_id)
            assert binding is not None and binding.volume_handle == 'volume-uid'
    finally:
        with sessions.begin() as db:
            binding = db.get(WorkspaceVolumeBinding, env_id)
            intent = db.get(WorkspaceProvisioningIntent, env_id)
            if binding is not None:
                db.delete(binding)
            if intent is not None:
                db.delete(intent)


def test_parallel_login_provisions_only_one_key(postgres_harness, monkeypatch):
    import httpx
    from cryptography.fernet import Fernet
    from control_plane import model_credentials
    from test_litellm import FakeGateway

    client, token = postgres_harness
    fake = FakeGateway()
    http_client = httpx.Client
    monkeypatch.setenv('PLATFORM_LITELLM_URL', 'http://litellm.test')
    monkeypatch.setenv('PLATFORM_LITELLM_MASTER_KEY', 'master')
    monkeypatch.setenv('PLATFORM_LITELLM_MODELS', 'model-a')
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('PLATFORM_LIBRECHAT_SERVICE_TOKEN', 's' * 40)
    get_settings.cache_clear()
    monkeypatch.setattr(model_credentials.httpx, 'Client', lambda **kw: http_client(transport=httpx.MockTransport(fake), **kw))
    headers = {'Authorization': 'Bearer ' + token('parallel-user'), 'X-CodeChat-Service-Token': 's' * 40}
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: client.post('/internal/v1/litellm/key', headers=headers), range(12)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()['key'] for response in responses}) == 1
    assert fake.calls.count(('POST', '/user/new')) == 1
    assert fake.calls.count(('POST', '/key/generate')) == 1
    with client.session_factory.begin() as db:
        environments = db.scalars(select(UserEnvironment).where(UserEnvironment.subject == 'parallel-user')).all()
        assert len(environments) == 1
        assert environments[0].first_project_id is None
        assert environments[0].status == 'requested'
        # This fixture uses a guarded, disposable local database only.
        db.delete(db.get(WorkspaceProvisioningIntent, environments[0].id))
        db.delete(db.get(CapacityAccount, environments[0].id))
        db.flush()
        db.delete(environments[0])


def test_parallel_capacity_orders_and_confirmations_are_idempotent(postgres_harness, monkeypatch):
    from sqlalchemy import delete, func
    from control_plane.models import CapacityAccount, CapacityOrder, StorageMeasurement, WorkspaceFileService
    from test_capacity import prepare_capacity, create_order, PREFIX
    client, _ = postgres_harness
    client, auth, env_id, _, _ = prepare_capacity(client, monkeypatch)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: create_order(client, auth), range(16)))
        assert all(response.status_code == 201 for response in responses)
        ids = {response.json()['id'] for response in responses}
        assert len(ids) == 1
        path = PREFIX + '/orders/' + ids.pop() + '/confirm-test'
        with ThreadPoolExecutor(max_workers=8) as pool:
            confirmations = list(pool.map(lambda _: client.post(path, headers=auth), range(16)))
        assert all(response.status_code == 200 and response.json()['attempt'] == 1 for response in confirmations)
        with client.session_factory() as db:
            assert db.scalar(select(func.count(CapacityOrder.id)).where(CapacityOrder.environment_id == env_id)) == 1
            assert db.get(UserEnvironment, env_id).storage_gib == 2
    finally:
        with client.session_factory.begin() as db:
            for model in (StorageMeasurement, CapacityOrder, CapacityAccount, WorkspaceFileService, WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id == env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))


def test_parallel_publications_cannot_overbook_compute(postgres_harness, monkeypatch):
    import httpx
    from sqlalchemy import delete
    from control_plane.models import CapacityAccount, UserApplication, StorageMeasurement, WorkspaceFileService
    from test_capacity import prepare_capacity, PREFIX
    client, _ = postgres_harness
    client, auth, env_id, _, _ = prepare_capacity(client, monkeypatch)
    monkeypatch.setenv('PLATFORM_PUBLISHER_ENABLED', 'true')
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.applications.call_files', lambda *a: httpx.Response(200,
        json={'entries': [{'name': 'Dockerfile', 'kind': 'file'}]}))
    client.get(PREFIX, headers=auth)
    with client.session_factory.begin() as db:
        account = db.get(CapacityAccount, env_id)
        account.plan = {**account.plan, 'application_limit': 20, 'applications_cpu_millicores': 500,
                        'applications_memory_mib': 512, 'build_concurrency': 4}
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda index: client.post('/internal/v1/workspace/apps',
                params={'path': f'project-{index}'}, headers=auth).status_code, range(16)))
        assert statuses.count(202) == 2 and statuses.count(409) == 14
        value = client.get(PREFIX, headers=auth).json()['quota_usage']
        assert value['application_cpu_millicores'] == 500
        assert value['application_memory_mib'] == 512
    finally:
        with client.session_factory.begin() as db:
            for model in (UserApplication, StorageMeasurement, CapacityAccount, WorkspaceFileService,
                          WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id == env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))


def test_parallel_project_association_and_revision(postgres_harness, monkeypatch):
    import httpx
    from cryptography.fernet import Fernet
    from control_plane.models import WorkspaceProject, StorageMeasurement, WorkspaceFileService
    from test_capacity import prepare_capacity
    client, _ = postgres_harness
    client, auth, env_id, _, _ = prepare_capacity(client, monkeypatch)
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.workspace_projects.call_files', lambda *a, **k: httpx.Response(200, json={'entries': []}))
    url = '/internal/v1/workspace/projects'
    body = {'path': 'existing', 'name': 'Team tool', 'goal': '', 'revision': 0}
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda _: client.put(url, headers=auth, json=body), range(12)))
        assert all(reply.status_code == 200 for reply in replies)
        assert len({reply.json()['id'] for reply in replies}) == 1
        identifier = replies[0].json()['id']
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda n: client.put(url, headers=auth, json={**body, 'goal': str(n), 'revision': 1}), range(6)))
        assert sum(reply.status_code == 200 for reply in replies) == 1
        assert sum(reply.status_code == 409 for reply in replies) == 5
        assert client.get(url+'/'+identifier, headers=auth).json()['revision'] == 2
    finally:
        with client.session_factory.begin() as db:
            for model in (WorkspaceProject, StorageMeasurement, CapacityAccount, WorkspaceFileService,
                          WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id == env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))


def test_projectless_migration_preserves_existing_workspace_binding(postgres_harness):
    client, _token = postgres_harness
    sessions = client.session_factory
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    with sessions.begin() as db:
        tenant = Tenant(name='migration', owner_issuer='issuer', owner_subject='migration', project_limit=1)
        db.add(tenant)
        db.flush()
        env = UserEnvironment(issuer='issuer', subject='migration', identity_hash='b' * 64,
                              storage_gib=2, first_tenant_id=tenant.id, first_project_id='existing-project')
        db.add(env)
        db.flush()
        env_id = env.id
        db.add(WorkspaceProvisioningIntent(environment_id=env_id, state='pvc_recorded'))
        db.add(WorkspaceVolumeBinding(environment_id=env_id, namespace_name='existing', namespace_uid='ns',
                                      pvc_uid='pvc', pv_name='pv', pv_uid='pv-uid', volume_handle='handle'))
    try:
        command.downgrade(config, '0007_workspace_isolation')
        command.upgrade(config, 'head')
        with sessions() as db:
            assert db.get(UserEnvironment, env_id).first_project_id == 'existing-project'
            assert db.get(WorkspaceProvisioningIntent, env_id).state == 'pvc_recorded'
            assert db.get(WorkspaceVolumeBinding, env_id).volume_handle == 'handle'
    finally:
        with sessions.begin() as db:
            for model in (WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                record = db.get(model, env_id)
                if record is not None:
                    db.delete(record)


def test_concurrent_signed_payment_notifications_confirm_once(postgres_harness, monkeypatch):
    from test_capacity import prepare_capacity, create_order, PREFIX
    from test_payments import local_payment, notification
    from control_plane.models import PaymentReceipt, CapacityOrder, AuditEvent, StorageMeasurement, WorkspaceFileService
    from sqlalchemy import func
    client, _ = postgres_harness
    _, auth, env_id, *_ = prepare_capacity(client, monkeypatch)
    try:
        with local_payment(monkeypatch):
            order = create_order(client, auth).json()
            assert client.post(PREFIX + '/orders/' + order['id'] + '/checkout', headers=auth).status_code == 200
            body = notification(order)
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: client.post('/payments/xunhupay/notify', data=body), range(8)))
            assert all(row.status_code == 200 and row.text == 'success' for row in results)
            with client.session_factory() as db:
                assert db.get(CapacityOrder, order['id']).attempt == 1
                assert db.get(PaymentReceipt, order['id']).state == 'confirmed'
                assert db.scalar(select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.action == 'payment.confirmed', AuditEvent.target_id == order['id'])) == 1
    finally:
        with client.session_factory.begin() as db:
            ids = select(CapacityOrder.id).where(CapacityOrder.environment_id == env_id)
            db.execute(delete(PaymentReceipt).where(PaymentReceipt.order_id.in_(ids)))
            db.execute(delete(CapacityAccount).where(CapacityAccount.environment_id == env_id))
            db.execute(delete(CapacityOrder).where(CapacityOrder.environment_id == env_id))
            for model in (StorageMeasurement, WorkspaceFileService, WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model).where(model.environment_id == env_id))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))
