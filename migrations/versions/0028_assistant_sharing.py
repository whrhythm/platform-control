"""Preserve existing grants; new invitations require explicit acceptance."""
from alembic import op
import sqlalchemy as sa

revision = '0028_assistant_sharing'
down_revision = '0027_knowledge_libraries'
branch_labels = depends_on = None


def upgrade():
    op.add_column('ai_releases', sa.Column('name', sa.String(80), nullable=True))
    op.add_column('ai_app_members', sa.Column('state', sa.String(24), nullable=False, server_default='active'))
    op.add_column('ai_app_members', sa.Column('revision', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('ai_app_members', sa.Column('budget_micros', sa.BigInteger(), nullable=True))
    op.add_column('ai_app_members', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE ai_app_members SET state='revoked' WHERE enabled=false")


def downgrade():
    # Old code cannot enforce acceptance, expiry or per-member limits.
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM ai_app_members')):
        raise RuntimeError('assistant sharing policies require forward recovery')
    for column in ('expires_at', 'budget_micros', 'revision', 'state'):
        op.drop_column('ai_app_members', column)
    op.drop_column('ai_releases', 'name')
