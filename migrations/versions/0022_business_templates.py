"""Business template creation journal and source-bound manual acceptance."""
from alembic import op
import sqlalchemy as sa
revision = '0022_business_templates'
down_revision = '0021_payment_receipts'
branch_labels = depends_on = None


def upgrade():
    op.create_table('business_templates',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('workspace_projects.id'), primary_key=True),
        sa.Column('template_id', sa.String(40), nullable=False),
        sa.Column('version', sa.String(40), nullable=False),
        sa.Column('state', sa.String(40), nullable=False),
        sa.Column('encrypted_access', sa.Text(), nullable=False),
        sa.Column('manifest', sa.JSON(), nullable=False),
        sa.Column('acceptance', sa.JSON(), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM business_templates')).scalar():
        raise RuntimeError('Preserve business template records before downgrade')
    op.drop_table('business_templates')
