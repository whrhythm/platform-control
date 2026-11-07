"""Immutable project knowledge intents and measured logical storage."""
from alembic import op
import sqlalchemy as sa

revision = '0024_project_knowledge'
down_revision = '0023_ai_project_bindings'
branch_labels = depends_on = None


def upgrade():
    op.add_column('ai_project_bindings', sa.Column('knowledge_revision_id', sa.String(36)))
    op.create_table('project_knowledge',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('ai_project_bindings.project_id'), nullable=False),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('request_id', sa.String(36), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('encrypted_source', sa.Text(), nullable=False),
        sa.Column('manifest', sa.JSON(), nullable=False),
        sa.Column('source_bytes', sa.BigInteger(), nullable=False),
        sa.Column('index_bytes', sa.BigInteger(), nullable=False),
        sa.Column('chunks', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('error_code', sa.String(60)),
        sa.Column('lease_token', sa.String(36)),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('project_id', 'request_id', name='uq_knowledge_request'))
    for column in ('project_id', 'environment_id', 'state'):
        op.create_index('ix_project_knowledge_'+column, 'project_knowledge', [column])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM project_knowledge')).scalar():
        raise RuntimeError('Preserve knowledge records before downgrade')
    op.drop_table('project_knowledge')
    op.drop_column('ai_project_bindings', 'knowledge_revision_id')
