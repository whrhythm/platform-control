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
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0024_project_knowledge"
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
