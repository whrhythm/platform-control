"""Private project/Agent binding intents, additive and owner scoped."""
from alembic import op
import sqlalchemy as sa

revision = '0023_ai_project_bindings'
down_revision = '0022_business_templates'
branch_labels = depends_on = None


def upgrade():
    op.create_table('ai_project_bindings',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('workspace_projects.id'), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('request_id', sa.String(36), nullable=False),
        sa.Column('native_owner_id', sa.String(24), nullable=False),
        sa.Column('agent_id', sa.String(80), nullable=False, unique=True),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('encrypted_initial', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('environment_id', 'request_id', name='uq_ai_project_request'))
    op.create_index('ix_ai_project_bindings_environment_id', 'ai_project_bindings', ['environment_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM ai_project_bindings')).scalar():
        raise RuntimeError('Preserve AI project binding records before downgrade')
    op.drop_table('ai_project_bindings')
