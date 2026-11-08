"""Persist confirmed project deletion plans across process restarts."""
from alembic import op
import sqlalchemy as sa

revision = '0029_ai_project_deletion'
down_revision = '0028_assistant_sharing'
branch_labels = depends_on = None


def upgrade():
    op.create_table('ai_project_deletions',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('ai_project_bindings.project_id'), primary_key=True),
        sa.Column('plan', sa.JSON(), nullable=False))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM ai_project_deletions')):
        raise RuntimeError('project deletion requires forward recovery')
    op.drop_table('ai_project_deletions')
