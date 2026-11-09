"""Pin embedding configurations to new revisions; null preserves legacy local indexes."""
from alembic import op
import sqlalchemy as sa

revision = '0031_embedding_profiles'
down_revision = '0030_code_project_deletion'
branch_labels = depends_on = None


def upgrade():
    op.create_table('embedding_profiles',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('url', sa.String(500), nullable=False),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('request_dimensions', sa.Boolean(), nullable=False),
        sa.Column('encrypted_secret', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('embedding_preference',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('profile_id', sa.String(36), sa.ForeignKey('embedding_profiles.id')),
        sa.Column('version', sa.Integer(), nullable=False))
    op.execute('INSERT INTO embedding_preference (id, version) VALUES (1, 0)')
    with op.batch_alter_table('project_knowledge') as batch:
        batch.add_column(sa.Column('embedding_profile_id', sa.String(36)))
        batch.create_foreign_key('fk_project_knowledge_embedding_profile',
                                 'embedding_profiles', ['embedding_profile_id'], ['id'])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM embedding_profiles')):
        raise RuntimeError('embedding profiles require forward recovery')
    with op.batch_alter_table('project_knowledge') as batch:
        batch.drop_constraint('fk_project_knowledge_embedding_profile', type_='foreignkey')
        batch.drop_column('embedding_profile_id')
    op.drop_table('embedding_preference')
    op.drop_table('embedding_profiles')
