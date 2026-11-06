"""Bounded encrypted source recovery points and durable restore intent."""
from alembic import op
import sqlalchemy as sa
revision = '0019_project_versions'
down_revision = '0018_code_execution'
branch_labels = depends_on = None


def upgrade():
    op.create_table('project_versions',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('environment_id',sa.String(36),sa.ForeignKey('user_environments.id'),nullable=False),
        sa.Column('request_key',sa.String(128),nullable=False),
        sa.Column('project_path',sa.String(255),nullable=False),
        sa.Column('name',sa.String(80),nullable=False),
        sa.Column('digest',sa.String(64),nullable=False),
        sa.Column('manifest',sa.JSON(),nullable=False),
        sa.Column('encrypted_content',sa.LargeBinary(),nullable=False),
        sa.Column('stored_bytes',sa.BigInteger(),nullable=False),
        sa.Column('source_bytes',sa.BigInteger(),nullable=False),
        sa.Column('deleted',sa.Boolean(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('environment_id','request_key',name='uq_project_version_request'))
    op.create_index('ix_project_versions_environment_id','project_versions',['environment_id'])
    op.create_table('project_restores',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('environment_id',sa.String(36),sa.ForeignKey('user_environments.id'),nullable=False),
        sa.Column('version_id',sa.String(36),sa.ForeignKey('project_versions.id'),nullable=False),
        sa.Column('request_key',sa.String(128),nullable=False),
        sa.Column('target',sa.String(255),nullable=False),
        sa.Column('status',sa.String(16),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('environment_id','request_key',name='uq_project_restore_request'))
    op.create_index('ix_project_restores_environment_id','project_restores',['environment_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM project_versions')).scalar():
        raise RuntimeError('Preserve source recovery points before downgrade')
    op.drop_table('project_restores')
    op.drop_table('project_versions')
