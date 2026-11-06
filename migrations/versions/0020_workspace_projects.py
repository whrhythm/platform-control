"""Owner-scoped project metadata. Existing folders, tasks and applications are unchanged."""
from alembic import op
import sqlalchemy as sa

revision = '0020_workspace_projects'
down_revision = '0019_project_versions'
branch_labels = depends_on = None


def upgrade():
    op.create_table('workspace_projects',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('project_path', sa.String(255), nullable=False),
        sa.Column('name', sa.String(80), nullable=False),
        sa.Column('encrypted_goal', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('environment_id', 'project_path', name='uq_workspace_project_path'))
    op.create_index('ix_workspace_projects_environment_id', 'workspace_projects', ['environment_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM workspace_projects')).scalar():
        raise RuntimeError('Preserve project metadata before downgrade')
    op.drop_table('workspace_projects')
