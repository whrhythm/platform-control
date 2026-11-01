"""Record each verified user's dedicated execution environment plan."""
from alembic import op
import sqlalchemy as sa

revision = "0004_user_environments"
down_revision = "0003_model_credentials"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_environments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("storage_gib", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("first_tenant_id", sa.String(36), nullable=False),
        sa.Column("first_project_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["first_tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint("issuer", "subject"),
        sa.CheckConstraint("status IN ('requested', 'provisioning', 'ready', 'suspended', 'deleted')", name="ck_user_environments_status"),
    )
    op.create_index("ix_user_environments_first_tenant_id", "user_environments", ["first_tenant_id"])
    op.create_index("ix_user_environments_first_project_id", "user_environments", ["first_project_id"])


def downgrade():
    op.drop_index("ix_user_environments_first_project_id", table_name="user_environments")
    op.drop_index("ix_user_environments_first_tenant_id", table_name="user_environments")
    op.drop_table("user_environments")
