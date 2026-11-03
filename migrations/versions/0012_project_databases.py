"""Owned project databases, provider identity and observed usage."""
from alembic import op
import sqlalchemy as sa

revision = '0012_project_databases'
down_revision = '0011_capacity_accounts'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('project_databases',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('project_path', sa.String(255), nullable=False),
        sa.Column('generation', sa.String(36), nullable=False),
        sa.Column('encrypted_password', sa.String(512), nullable=False),
        sa.Column('reserved_bytes', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('resources', sa.JSON(), nullable=False),
        sa.Column('used_bytes', sa.BigInteger(), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('environment_id', 'project_path'))
    op.create_index('ix_project_databases_environment_id', 'project_databases', ['environment_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM project_databases')).scalar():
        raise RuntimeError('Cannot discard database ownership records')
    op.drop_table('project_databases')
