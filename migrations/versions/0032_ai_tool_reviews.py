"""Persist project-scoped personal tool reviews and release grants."""
from alembic import op
import sqlalchemy as sa

revision = '0032_ai_tool_reviews'
down_revision = '0031_embedding_profiles'
branch_labels = depends_on = None


def upgrade():
    op.create_table('ai_tool_reviews',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('ai_project_bindings.project_id'), nullable=False),
        sa.Column('reference', sa.String(140), nullable=False),
        sa.Column('descriptor', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('project_id', 'reference'))
    op.create_index('ix_ai_tool_reviews_project_id', 'ai_tool_reviews', ['project_id'])
    with op.batch_alter_table('ai_releases') as batch:
        batch.add_column(sa.Column('tool_review_id', sa.String(36)))
        batch.create_foreign_key('fk_ai_release_tool_review', 'ai_tool_reviews', ['tool_review_id'], ['id'])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM ai_tool_reviews')):
        raise RuntimeError('Tool review history requires forward recovery')
    with op.batch_alter_table('ai_releases') as batch:
        batch.drop_constraint('fk_ai_release_tool_review', type_='foreignkey')
        batch.drop_column('tool_review_id')
    op.drop_index('ix_ai_tool_reviews_project_id', table_name='ai_tool_reviews')
    op.drop_table('ai_tool_reviews')
