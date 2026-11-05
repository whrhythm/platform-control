"""Platform account access fence independent of persistent user data."""
from alembic import op
import sqlalchemy as sa
revision = '0017_account_access'
down_revision = '0016_mcp_catalog'
branch_labels = depends_on = None

def upgrade():
    op.create_table('account_access', sa.Column('issuer', sa.String(512), primary_key=True),
                    sa.Column('subject', sa.String(255), primary_key=True), sa.Column('blocked', sa.Boolean(), nullable=False))

def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM account_access')).scalar():
        raise RuntimeError('Account access decisions must be preserved before downgrade')
    op.drop_table('account_access')
