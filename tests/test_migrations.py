from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from control_plane.database import make_engine
from control_plane.models import Base
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
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004_user_environments"
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
