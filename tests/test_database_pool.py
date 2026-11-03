from control_plane.database import make_engine
from control_plane.settings import get_settings


def test_background_pool_obeys_configured_bound_without_connecting(monkeypatch):
    monkeypatch.setenv('PLATFORM_DATABASE_POOL_SIZE', '1')
    monkeypatch.setenv('PLATFORM_DATABASE_POOL_OVERFLOW', '1')
    get_settings.cache_clear()
    try:
        engine = make_engine('postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/test')
        assert engine.pool.size() == 1 and engine.pool._max_overflow == 1
        engine.dispose()
        local = make_engine('sqlite:///:memory:')
        local.dispose()
    finally:
        get_settings.cache_clear()
