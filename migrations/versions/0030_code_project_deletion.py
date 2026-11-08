"""Persist confirmed code project retirement plans."""
from alembic import op
import sqlalchemy as sa

revision = '0030_code_project_deletion'
down_revision = '0029_ai_project_deletion'
branch_labels = depends_on = None


def upgrade():
    op.create_table('code_project_deletions',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('workspace_projects.id'), primary_key=True),
        sa.Column('plan', sa.JSON(), nullable=False),
        sa.Column('completed', sa.Boolean(), nullable=False))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM code_project_deletions')):
        raise RuntimeError('code project deletion requires forward recovery')
    op.drop_table('code_project_deletions')
