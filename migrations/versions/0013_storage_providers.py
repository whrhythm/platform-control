"""Sealed private object storage and attributed provider measurements."""
from alembic import op
import sqlalchemy as sa

revision = '0013_storage_providers'
down_revision = '0012_project_databases'
branch_labels = None
depends_on = None


def upgrade():
    for column in (sa.Column('budget_bytes', sa.BigInteger(), nullable=True),
                   sa.Column('basis', sa.String(64), nullable=True),
                   sa.Column('scope_id', sa.String(255), nullable=True)):
        op.add_column('storage_measurements', column)
    op.create_table('user_object_storage',
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), primary_key=True),
        sa.Column('generation', sa.String(36), nullable=False),
        sa.Column('bucket_name', sa.String(63), nullable=False, unique=True),
        sa.Column('access_key', sa.String(32), nullable=False, unique=True),
        sa.Column('encrypted_secret', sa.String(512), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('resources', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM user_object_storage')).scalar():
        raise RuntimeError('Cannot discard object storage ownership records')
    op.drop_table('user_object_storage')
    for column in ('scope_id', 'basis', 'budget_bytes'):
        op.drop_column('storage_measurements', column)
