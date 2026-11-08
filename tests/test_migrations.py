from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
import pytest

from control_plane.database import make_engine
from control_plane.models import Base, Tenant, UserEnvironment, WorkspaceVolumeBinding, WorkspaceProvisioningIntent
from control_plane.settings import get_settings


def assert_library_backfill_preserves_pinned_data(config, engine):
    from uuid import uuid4
    from datetime import timedelta
    from sqlalchemy import delete
    from control_plane.models import (WorkspaceProject, AIProjectBinding, ProjectKnowledge,
        KnowledgeLibrary, AIProjectTrial, AIApplication, AIRelease, now)
    from control_plane.knowledge import revision_scope
    # Seed the old shape through nullable compatibility columns, then remove them.
    with Session(engine) as db:
        tenant = Tenant(name='migration', owner_issuer='issuer', owner_subject='migration', project_limit=1)
        db.add(tenant); db.flush()
        env = UserEnvironment(issuer='issuer',subject='migration',identity_hash='e'*64,
            storage_gib=2,first_tenant_id=tenant.id,first_project_id=None)
        db.add(env); db.flush()
        project = WorkspaceProject(environment_id=env.id,project_path='existing',name='原知识库',encrypted_goal='unchanged')
        db.add(project); db.flush()
        binding = AIProjectBinding(project_id=project.id,environment_id=env.id,request_id=str(uuid4()),
            native_owner_id='a'*24,agent_id='agent_'+project.id.replace('-',''),state='ready',encrypted_initial='original')
        db.add(binding); db.flush()
        first = ProjectKnowledge(project_id=project.id,environment_id=env.id,request_id=str(uuid4()),
            digest='a'*64,encrypted_source='original-encrypted-source',manifest=[{'name':'policy.md','bytes':10}],
            source_bytes=10,index_bytes=100,chunks=1,state='ready',created_at=now()-timedelta(seconds=3))
        second = ProjectKnowledge(project_id=project.id,environment_id=env.id,request_id=str(uuid4()),
            digest='b'*64,encrypted_source='new-encrypted-source',manifest=[{'name':'policy.md','bytes':20}],
            source_bytes=20,index_bytes=100,chunks=1,state='failed')
        db.add_all([first,second]); db.flush()
        binding.knowledge_revision_id = first.id
        trial = AIProjectTrial(project_id=project.id,environment_id=env.id,request_id=str(uuid4()),
            number=1,digest='c'*64,encrypted_snapshot='unchanged-trial-snapshot',knowledge_revision_id=first.id,state='ready')
        db.add(trial); db.flush()
        db.add(AIApplication(project_id=project.id,budget_micros=1000000)); db.flush()
        release = AIRelease(project_id=project.id,trial_id=trial.id,request_id=str(uuid4()),number=1)
        db.add(release); db.flush()
        db.get(AIApplication,project.id).active_release_id=release.id
        ids = (project.id,first.id,second.id,trial.id,release.id,env.id,tenant.id)
        db.commit()
    try:
        command.downgrade(config,'0026_ai_releases')
        command.upgrade(config,'head')
        with Session(engine) as db:
            p,first_id,second_id,trial_id,release_id,*_ = ids
            library = db.get(KnowledgeLibrary,p)
            assert library.name == '原知识库'
            assert library.head_revision_id == second_id and library.active_revision_id == first_id
            assert db.get(AIProjectBinding,p).knowledge_library_id == p
            old = db.get(ProjectKnowledge,first_id)
            assert old.library_id == p and revision_scope(old) == p
            assert old.encrypted_source == 'original-encrypted-source' and old.source_bytes == 10
            assert db.get(AIProjectTrial,trial_id).encrypted_snapshot == 'unchanged-trial-snapshot'
            assert db.get(AIProjectTrial,trial_id).knowledge_revision_id == first_id
            assert db.get(AIApplication,p).active_release_id == release_id
        with pytest.raises(RuntimeError,match='Preserve libraries'):
            command.downgrade(config,'0026_ai_releases')
    finally:
        with Session(engine) as db:
            p,_,_,_,_,env_id,tenant_id = ids
            for model in (AIRelease,AIApplication,AIProjectTrial,ProjectKnowledge,AIProjectBinding):
                db.execute(delete(model).where(model.project_id==p))
            db.execute(delete(KnowledgeLibrary).where(KnowledgeLibrary.id==p))
            db.execute(delete(WorkspaceProject).where(WorkspaceProject.id==p))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id==env_id))
            db.execute(delete(Tenant).where(Tenant.id==tenant_id))
            db.commit()


def test_library_backfill_preserves_existing_snapshots(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'library-upgrade.db'}"
    monkeypatch.setenv('PLATFORM_DATABASE_URL',url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    engine = make_engine(url)
    try:
        command.upgrade(config,'head')
        assert_library_backfill_preserves_pinned_data(config,engine)
    finally:
        engine.dispose(); get_settings.cache_clear()


def test_versioned_schema_upgrade_matches_models_and_roundtrips(tmp_path, monkeypatch):
    import control_plane.main as main
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("PLATFORM_DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    engine = make_engine(url)
    try:
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0027_knowledge_libraries"
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        monkeypatch.setattr(main, "engine", engine)
        with TestClient(main.app) as client:
            assert client.get("/healthz").status_code == 200
        command.downgrade(config, "base")
        assert inspect(engine).get_table_names() == ["alembic_version"]
        command.upgrade(config, "head")
        assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_downgrade_cannot_discard_recorded_volume_identity(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'protected.db'}"
    monkeypatch.setenv("PLATFORM_DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    engine = make_engine(url)
    try:
        command.upgrade(config, '0007_workspace_isolation')
        with Session(engine) as db:
            tenant = Tenant(name='test', owner_issuer='issuer', owner_subject='alice', project_limit=1)
            db.add(tenant)
            db.flush()
            env = UserEnvironment(issuer='issuer', subject='alice', identity_hash='a' * 64, storage_gib=2,
                                  first_tenant_id=tenant.id, first_project_id='project')
            db.add(env)
            db.flush()
            db.add(WorkspaceVolumeBinding(environment_id=env.id, namespace_name='test-ns', namespace_uid='ns-uid',
                                          pvc_uid='pvc-uid', pv_name='pv', pv_uid='pv-uid', volume_handle='volume'))
            db.commit()
        with pytest.raises(RuntimeError, match='Cannot remove nonempty'):
            command.downgrade(config, '0004_user_environments')
        with engine.connect() as connection:
            assert connection.scalar(text('SELECT COUNT(*) FROM workspace_volume_bindings')) == 1
            assert connection.scalar(text('SELECT version_num FROM alembic_version')) == '0005_workspace_bindings'
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_downgrade_cannot_discard_inflight_creation(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'intent-protected.db'}"
    monkeypatch.setenv('PLATFORM_DATABASE_URL', url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    engine = make_engine(url)
    try:
        command.upgrade(config, '0007_workspace_isolation')
        with Session(engine) as db:
            tenant = Tenant(name='test', owner_issuer='issuer', owner_subject='alice', project_limit=1)
            db.add(tenant)
            db.flush()
            env = UserEnvironment(issuer='issuer', subject='alice', identity_hash='a' * 64, storage_gib=2,
                                  first_tenant_id=tenant.id, first_project_id='project')
            db.add(env)
            db.flush()
            db.add(WorkspaceProvisioningIntent(environment_id=env.id, state='namespace_inflight'))
            db.commit()
        with pytest.raises(RuntimeError, match='Cannot downgrade nonempty'):
            command.downgrade(config, '0005_workspace_bindings')
        with engine.connect() as connection:
            assert connection.scalar(text('SELECT state FROM workspace_provisioning_intents')) == 'namespace_inflight'
            assert connection.scalar(text('SELECT version_num FROM alembic_version')) == '0007_workspace_isolation'
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_projectless_environment_blocks_unsafe_downgrade(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'projectless.db'}"
    monkeypatch.setenv('PLATFORM_DATABASE_URL', url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    engine = make_engine(url)
    try:
        command.upgrade(config, 'head')
        with Session(engine) as db:
            tenant = Tenant(name='test', owner_issuer='issuer', owner_subject='alice', project_limit=1)
            db.add(tenant)
            db.flush()
            db.add(UserEnvironment(issuer='issuer', subject='alice', identity_hash='a' * 64,
                                   storage_gib=2, first_tenant_id=tenant.id, first_project_id=None))
            db.commit()
        with pytest.raises(RuntimeError, match='without a project'):
            command.downgrade(config, '0007_workspace_isolation')
        with engine.connect() as connection:
            assert connection.scalar(text('SELECT COUNT(*) FROM user_environments')) == 1
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_sqlite_upgrade_refuses_to_rebuild_referenced_environments(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'referenced.db'}"
    monkeypatch.setenv('PLATFORM_DATABASE_URL', url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    engine = make_engine(url)
    try:
        command.upgrade(config, '0007_workspace_isolation')
        with Session(engine) as db:
            tenant = Tenant(name='test', owner_issuer='issuer', owner_subject='alice', project_limit=1)
            db.add(tenant)
            db.flush()
            env = UserEnvironment(issuer='issuer', subject='alice', identity_hash='a' * 64, storage_gib=2,
                                  first_tenant_id=tenant.id, first_project_id='project')
            db.add(env)
            db.flush()
            db.add(WorkspaceProvisioningIntent(environment_id=env.id, state='requested'))
            db.commit()
        with pytest.raises(RuntimeError, match='SQLite migration requires'):
            command.upgrade(config, 'head')
        with engine.connect() as connection:
            assert connection.scalar(text('SELECT COUNT(*) FROM user_environments')) == 1
            assert connection.scalar(text('SELECT COUNT(*) FROM workspace_provisioning_intents')) == 1
            assert connection.scalar(text('SELECT version_num FROM alembic_version')) == '0007_workspace_isolation'
    finally:
        engine.dispose()
        get_settings.cache_clear()
