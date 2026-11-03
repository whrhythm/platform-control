"""Administrator model policy, observed wallet and durable credit ledger."""
from alembic import op
import sqlalchemy as sa

revision = '0014_ai_membership'
down_revision = '0013_storage_providers'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ai_configuration', sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False), sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('catalog', sa.JSON(), nullable=False), sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('model_credits', sa.Column('order_id', sa.String(36), sa.ForeignKey('capacity_orders.id'), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False, index=True),
        sa.Column('amount_micros', sa.BigInteger(), nullable=False), sa.Column('status', sa.String(32), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('model_wallets', sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), primary_key=True),
        sa.Column('sample', sa.JSON(), nullable=False), sa.Column('status', sa.String(32), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM model_credits')).scalar():
        raise RuntimeError('Cannot discard model credit ledger')
    op.drop_table('model_wallets')
    op.drop_table('model_credits')
    op.drop_table('ai_configuration')
