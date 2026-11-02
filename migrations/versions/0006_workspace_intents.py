"""Persist create-only intent before contacting a workspace provider."""
from alembic import op
import sqlalchemy as sa

revision = "0006_workspace_intents"
down_revision = "0005_workspace_bindings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'workspace_provisioning_intents',
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), primary_key=True),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('namespace_uid', sa.String(128), nullable=True, unique=True),
        sa.Column('pvc_uid', sa.String(128), nullable=True, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('requested', 'namespace_inflight', 'namespace_recorded', 'pvc_inflight', 'pvc_recorded')", name='ck_workspace_provisioning_state'),
    )


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM workspace_provisioning_intents')).scalar():
        raise RuntimeError('Cannot remove nonempty workspace provisioning intents')
    op.drop_table('workspace_provisioning_intents')
