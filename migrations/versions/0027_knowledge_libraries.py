"""Independent libraries; preserve old revision IDs and index scopes."""
from alembic import op
import sqlalchemy as sa

revision = '0027_knowledge_libraries'
down_revision = '0026_ai_releases'
branch_labels = depends_on = None


def upgrade():
    op.create_table('knowledge_libraries',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('request_id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(80), nullable=False),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('head_revision_id', sa.String(36)),
        sa.Column('active_revision_id', sa.String(36)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('environment_id', 'request_id', name='uq_library_request'))
    op.create_index('ix_knowledge_libraries_environment_id', 'knowledge_libraries', ['environment_id'])
    op.add_column('ai_project_bindings', sa.Column('knowledge_library_id', sa.String(36)))
    op.create_index('ix_ai_project_bindings_knowledge_library_id', 'ai_project_bindings', ['knowledge_library_id'])
    with op.batch_alter_table('project_knowledge') as batch:
        batch.alter_column('project_id', existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column('library_id', sa.String(36)))
        batch.add_column(sa.Column('edit_digest', sa.String(64)))
        batch.create_foreign_key('fk_revision_library', 'knowledge_libraries', ['library_id'], ['id'])
        batch.create_index('ix_project_knowledge_library_id', ['library_id'])
        batch.create_unique_constraint('uq_library_revision_request', ['library_id', 'request_id'])
    db = op.get_bind()
    db.execute(sa.text("""INSERT INTO knowledge_libraries
        (id,environment_id,request_id,name,state,version,head_revision_id,active_revision_id,created_at,updated_at)
        SELECT b.project_id,b.environment_id,b.project_id,p.name,'active',1,
          (SELECT k.id FROM project_knowledge k WHERE k.project_id=b.project_id AND k.state NOT IN ('deleted','deleting') ORDER BY k.created_at DESC,k.id DESC LIMIT 1),
          b.knowledge_revision_id,b.created_at,b.updated_at
        FROM ai_project_bindings b JOIN workspace_projects p ON p.id=b.project_id
        WHERE EXISTS(SELECT 1 FROM project_knowledge k WHERE k.project_id=b.project_id AND k.state NOT IN ('deleted','deleting'))"""))
    db.execute(sa.text('UPDATE ai_project_bindings SET knowledge_library_id=project_id WHERE project_id IN (SELECT id FROM knowledge_libraries)'))
    db.execute(sa.text('UPDATE project_knowledge SET library_id=project_id WHERE project_id IN (SELECT id FROM knowledge_libraries)'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM knowledge_libraries')).scalar():
        raise RuntimeError('Preserve libraries and pinned knowledge before downgrade')
    with op.batch_alter_table('project_knowledge') as batch:
        batch.drop_constraint('uq_library_revision_request', type_='unique')
        batch.drop_constraint('fk_revision_library', type_='foreignkey')
        batch.drop_index('ix_project_knowledge_library_id')
        batch.drop_column('library_id')
        batch.drop_column('edit_digest')
        batch.alter_column('project_id', existing_type=sa.String(36), nullable=False)
    op.drop_index('ix_ai_project_bindings_knowledge_library_id', table_name='ai_project_bindings')
    op.drop_column('ai_project_bindings', 'knowledge_library_id')
    op.drop_table('knowledge_libraries')
