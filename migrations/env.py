from alembic import context

from control_plane.database import make_engine
from control_plane.models import Base
from control_plane.settings import get_settings


def run():
    if context.is_offline_mode():
        context.configure(url=get_settings().database_url, target_metadata=Base.metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = make_engine(get_settings().database_url)
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
        engine.dispose()


run()
