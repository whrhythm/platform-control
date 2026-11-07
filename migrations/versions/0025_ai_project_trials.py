"""Fixed private trial snapshots and owner-scoped session receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0025_ai_project_trials'
down_revision = '0024_project_knowledge'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ai_project_drafts',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('ai_project_bindings.project_id'), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('encrypted_config', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('ai_project_trials',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('ai_project_bindings.project_id'), nullable=False),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('request_id', sa.String(36), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('encrypted_snapshot', sa.Text(), nullable=False),
        sa.Column('knowledge_revision_id', sa.String(36)),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('encrypted_acceptance', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('project_id', 'request_id'), sa.UniqueConstraint('project_id', 'number'))
    for key in ('project_id', 'environment_id'):
        op.create_index('ix_ai_project_trials_'+key, 'ai_project_trials', [key])
    op.create_table('ai_trial_sessions',
        sa.Column('conversation_id', sa.String(36), primary_key=True),
        sa.Column('trial_id', sa.String(36), sa.ForeignKey('ai_project_trials.id'), nullable=False),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_ai_trial_sessions_trial_id', 'ai_trial_sessions', ['trial_id'])
    op.create_table('ai_trial_runs',
        sa.Column('message_id', sa.String(100), primary_key=True),
        sa.Column('conversation_id', sa.String(36), sa.ForeignKey('ai_trial_sessions.conversation_id'), nullable=False),
        sa.Column('trial_id', sa.String(36), sa.ForeignKey('ai_project_trials.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    for key in ('conversation_id', 'trial_id'):
        op.create_index('ix_ai_trial_runs_'+key, 'ai_trial_runs', [key])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM ai_project_trials')).scalar():
        raise RuntimeError('Preserve trial evidence before downgrade')
    op.drop_table('ai_trial_runs')
    op.drop_table('ai_trial_sessions')
    op.drop_table('ai_project_trials')
    op.drop_table('ai_project_drafts')
