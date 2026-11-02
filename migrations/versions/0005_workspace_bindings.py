"""Persist immutable workspace volume identity, without provisioning resources."""
from alembic import op
import sqlalchemy as sa

revision = "0005_workspace_bindings"
down_revision = "0004_user_environments"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workspace_volume_bindings",
        sa.Column("environment_id", sa.String(36), sa.ForeignKey("user_environments.id"), primary_key=True),
        sa.Column("namespace_name", sa.String(63), nullable=False, unique=True),
        sa.Column("namespace_uid", sa.String(128), nullable=False, unique=True),
        sa.Column("pvc_uid", sa.String(128), nullable=False, unique=True),
        sa.Column("pv_name", sa.String(253), nullable=False, unique=True),
        sa.Column("pv_uid", sa.String(128), nullable=False, unique=True),
        sa.Column("volume_handle", sa.String(255), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    # Never silently discard the identifiers required to recover user volumes.
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM workspace_volume_bindings")).scalar():
        raise RuntimeError("Cannot remove nonempty workspace volume bindings")
    op.drop_table("workspace_volume_bindings")
