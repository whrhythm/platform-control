"""Add explicitly simulated payment orders; no real payment processing."""
from alembic import op
import sqlalchemy as sa

revision = "0002_test_orders"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "test_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
    )
    op.create_index("ix_test_orders_tenant_id", "test_orders", ["tenant_id"])


def downgrade():
    op.drop_index("ix_test_orders_tenant_id", table_name="test_orders")
    op.drop_table("test_orders")
