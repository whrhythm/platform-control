"""Persist one encrypted LiteLLM credential per verified user."""
from alembic import op
import sqlalchemy as sa

revision = "0003_model_credentials"
down_revision = "0002_test_orders"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("encrypted_key", sa.String(512), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("issuer", "subject"),
        sa.CheckConstraint("status IN ('pending', 'ready', 'disabled')", name="ck_model_credentials_status"),
    )


def downgrade():
    op.drop_table("model_credentials")
