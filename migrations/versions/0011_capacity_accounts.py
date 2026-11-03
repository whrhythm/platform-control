"""Durable test upgrades and source-aware storage measurements."""
from alembic import op
import sqlalchemy as sa

revision = '0011_capacity_accounts'
down_revision = '0010_application_delivery'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('capacity_accounts',
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), primary_key=True),
        sa.Column('plan', sa.JSON(), nullable=False),
        sa.Column('active_order_id', sa.String(36), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('capacity_orders',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('plan', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('payment_status', sa.String(16), nullable=False),
        sa.Column('stage', sa.String(40), nullable=False),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('environment_id', 'idempotency_key'),
        sa.CheckConstraint("status IN ('pending_payment', 'applying', 'ready', 'failed', 'cancelled')", name='ck_capacity_order_status'))
    op.create_index('ix_capacity_orders_environment_id', 'capacity_orders', ['environment_id'])
    op.create_table('storage_measurements',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('component', sa.String(24), nullable=False),
        sa.Column('used_bytes', sa.BigInteger(), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(24), nullable=False),
        sa.UniqueConstraint('environment_id', 'component'))
    op.create_index('ix_storage_measurements_environment_id', 'storage_measurements', ['environment_id'])


def downgrade():
    for table in ('storage_measurements', 'capacity_orders', 'capacity_accounts'):
        if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM ' + table)).scalar():
            raise RuntimeError('Cannot discard capacity ownership or order records')
    for table in ('storage_measurements', 'capacity_orders', 'capacity_accounts'):
        op.drop_table(table)
