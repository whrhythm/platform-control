"""Persistent private registry bindings and application delivery jobs."""
from alembic import op
import sqlalchemy as sa

revision = '0010_application_delivery'
down_revision = '0009_workspace_files'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('user_registries',
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), primary_key=True),
        sa.Column('project_name', sa.String(63), nullable=False, unique=True),
        sa.Column('project_id', sa.Integer(), nullable=True),
        sa.Column('robot_id', sa.Integer(), nullable=True),
        sa.Column('robot_name', sa.String(255), nullable=False),
        sa.Column('pull_secret_uid', sa.String(128), nullable=True),
        sa.Column('encrypted_secret', sa.String(1024), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('user_applications',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('project_path', sa.String(255), nullable=False),
        sa.Column('revision', sa.String(36), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('desired_state', sa.String(16), nullable=False),
        sa.Column('resources', sa.JSON(), nullable=False),
        sa.Column('image', sa.String(512), nullable=True),
        sa.Column('url', sa.String(512), nullable=True),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('observation', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('environment_id', 'project_path'))
    op.create_index('ix_user_applications_environment_id', 'user_applications', ['environment_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM user_registries')).scalar() or op.get_bind().execute(sa.text('SELECT COUNT(*) FROM user_applications')).scalar():
        raise RuntimeError('Cannot discard registry or delivery ownership records')
    op.drop_table('user_applications')
    op.drop_table('user_registries')
