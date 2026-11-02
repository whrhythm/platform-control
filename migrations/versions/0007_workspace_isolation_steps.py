"""Journal quota and network-policy creation before the workspace PVC."""
from alembic import op
import sqlalchemy as sa

revision = '0007_workspace_isolation'
down_revision = '0006_workspace_intents'
branch_labels = None
depends_on = None

OLD = "state IN ('requested', 'namespace_inflight', 'namespace_recorded', 'pvc_inflight', 'pvc_recorded')"
NEW = "state IN ('requested', 'namespace_inflight', 'namespace_recorded', 'quota_inflight', 'quota_recorded', 'policy_inflight', 'policy_recorded', 'pvc_inflight', 'pvc_recorded')"


def upgrade():
    with op.batch_alter_table('workspace_provisioning_intents') as batch:
        batch.add_column(sa.Column('quota_uid', sa.String(128), nullable=True))
        batch.add_column(sa.Column('policy_uid', sa.String(128), nullable=True))
        batch.create_unique_constraint('uq_workspace_intent_quota_uid', ['quota_uid'])
        batch.create_unique_constraint('uq_workspace_intent_policy_uid', ['policy_uid'])
        batch.drop_constraint('ck_workspace_provisioning_state', type_='check')
        batch.create_check_constraint('ck_workspace_provisioning_state', NEW)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM workspace_provisioning_intents')).scalar():
        raise RuntimeError('Cannot downgrade nonempty workspace provisioning intents')
    with op.batch_alter_table('workspace_provisioning_intents') as batch:
        batch.drop_constraint('ck_workspace_provisioning_state', type_='check')
        batch.create_check_constraint('ck_workspace_provisioning_state', OLD)
        batch.drop_constraint('uq_workspace_intent_policy_uid', type_='unique')
        batch.drop_constraint('uq_workspace_intent_quota_uid', type_='unique')
        batch.drop_column('policy_uid')
        batch.drop_column('quota_uid')
