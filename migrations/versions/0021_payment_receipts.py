"""Durable simulated payment intents; no live provider is enabled by this migration."""
from alembic import op
import sqlalchemy as sa
revision='0021_payment_receipts'
down_revision='0020_workspace_projects'
branch_labels=depends_on=None


def upgrade():
    op.create_table('payment_receipts',
        sa.Column('order_id',sa.String(36),sa.ForeignKey('capacity_orders.id'),primary_key=True),
        sa.Column('state',sa.String(24),nullable=False),
        sa.Column('transaction_id',sa.String(32),nullable=True,unique=True),
        sa.Column('checkout_url',sa.String(512),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('confirmed_at',sa.DateTime(timezone=True),nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM payment_receipts')).scalar():
        raise RuntimeError('Preserve payment records before downgrade')
    op.drop_table('payment_receipts')
