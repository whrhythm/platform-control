"""Durable owner-scoped programming settings, tasks and encrypted events."""
from alembic import op
import sqlalchemy as sa

revision = '0015_agent_tasks'
down_revision = '0014_ai_membership'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('agent_preferences',
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), primary_key=True),
        sa.Column('conversation_id', sa.String(64), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False), sa.Column('config', sa.JSON(), nullable=False))
    op.create_table('agent_tasks',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False, index=True),
        sa.Column('conversation_id', sa.String(64), nullable=False),
        sa.Column('request_key', sa.String(64), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('encrypted_prompt', sa.Text(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False, index=True),
        sa.Column('encrypted_events', sa.Text(), nullable=False),
        sa.Column('pending', sa.JSON(), nullable=True),
        sa.Column('resources', sa.JSON(), nullable=False),
        sa.Column('error_code', sa.String(80), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('environment_id', 'request_key'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM agent_tasks')).scalar():
        raise RuntimeError('Cannot discard programming task history')
    op.drop_table('agent_tasks')
    op.drop_table('agent_preferences')
