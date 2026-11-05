"""Durable code execution quota reservations."""
from alembic import op
import sqlalchemy as sa
revision = '0018_code_execution'
down_revision = '0017_account_access'
branch_labels = depends_on = None


def upgrade():
    op.create_table('code_executions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('request_key', sa.String(128), nullable=False), sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('operation', sa.String(12), nullable=False), sa.Column('transfer_bytes', sa.BigInteger(), nullable=False),
        sa.Column('period', sa.String(7), nullable=False), sa.Column('state', sa.String(20), nullable=False),
        sa.Column('timeout_seconds', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('environment_id', 'request_key', name='uq_code_execution_request'))
    op.create_index('ix_code_executions_environment_id', 'code_executions', ['environment_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM code_executions')).scalar():
        raise RuntimeError('Preserve execution accounting before downgrade')
    op.drop_table('code_executions')
