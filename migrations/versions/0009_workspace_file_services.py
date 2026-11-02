"""Persist file service identity and create-only resource journal."""
from alembic import op
import sqlalchemy as sa

revision = '0009_workspace_files'
down_revision = '0008_projectless_space'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('workspace_file_services',
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), primary_key=True),
        sa.Column('encrypted_token', sa.String(512), nullable=False),
        sa.Column('resources', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM workspace_file_services')).scalar():
        raise RuntimeError('Cannot discard nonempty workspace file service journal')
    op.drop_table('workspace_file_services')
