"""Allow an identity-bound environment to be requested before any project."""
from alembic import op
import sqlalchemy as sa

revision = '0008_projectless_space'
down_revision = '0007_workspace_isolation'
branch_labels = None
depends_on = None


def ensure_sqlite_has_no_references():
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        for table in ('workspace_volume_bindings', 'workspace_provisioning_intents'):
            if bind.execute(sa.text(f'SELECT COUNT(*) FROM {table}')).scalar():
                raise RuntimeError('SQLite migration requires no workspace bindings or provisioning intents')


def upgrade():
    ensure_sqlite_has_no_references()
    with op.batch_alter_table('user_environments') as batch:
        batch.alter_column('first_project_id', existing_type=sa.String(36), nullable=True)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM user_environments WHERE first_project_id IS NULL')).scalar():
        raise RuntimeError('Cannot downgrade environments without a project')
    ensure_sqlite_has_no_references()
    with op.batch_alter_table('user_environments') as batch:
        batch.alter_column('first_project_id', existing_type=sa.String(36), nullable=False)
