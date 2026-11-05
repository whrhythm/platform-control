"""Reviewed MCP services and encrypted per-owner credentials."""
from alembic import op
import sqlalchemy as sa

revision = '0016_mcp_catalog'
down_revision = '0015_agent_tasks'
branch_labels = depends_on = None


def upgrade():
    op.create_table('mcp_servers', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False), sa.Column('state', sa.String(16), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False), sa.Column('discovered', sa.JSON(), nullable=False),
        sa.Column('approved', sa.JSON(), nullable=False), sa.Column('addresses', sa.JSON(), nullable=False),
        sa.Column('encrypted_credential', sa.Text(), nullable=True), sa.Column('checked_at', sa.DateTime(timezone=True)))
    op.create_table('mcp_credentials',
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), primary_key=True),
        sa.Column('server_id', sa.String(64), sa.ForeignKey('mcp_servers.id'), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False), sa.Column('server_revision', sa.Integer(), nullable=False),
        sa.Column('encrypted_value', sa.Text(), nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM mcp_servers')).scalar():
        raise RuntimeError('Cannot discard reviewed MCP services or credentials')
    op.drop_table('mcp_credentials')
    op.drop_table('mcp_servers')
