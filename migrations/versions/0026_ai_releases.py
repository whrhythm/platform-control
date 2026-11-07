"""Published snapshots, members, private entries and sponsored invocation ledger."""
from alembic import op
import sqlalchemy as sa

revision = '0026_ai_releases'
down_revision = '0025_ai_project_trials'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ai_applications',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('ai_project_bindings.project_id'), primary_key=True),
        sa.Column('active_release_id', sa.String(36)), sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('budget_micros', sa.BigInteger(), nullable=False), sa.Column('revision', sa.Integer(), nullable=False))
    op.create_table('ai_releases',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('ai_applications.project_id'), nullable=False),
        sa.Column('trial_id', sa.String(36), sa.ForeignKey('ai_project_trials.id'), nullable=False),
        sa.Column('request_id', sa.String(36), nullable=False), sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('project_id','request_id'), sa.UniqueConstraint('project_id','number'))
    op.create_index('ix_ai_releases_project_id','ai_releases',['project_id'])
    op.create_table('ai_app_members',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('ai_applications.project_id'), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), primary_key=True),
        sa.Column('native_id', sa.String(24), nullable=False), sa.Column('encrypted_label', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False))
    op.create_table('ai_app_entries',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('release_id', sa.String(36), sa.ForeignKey('ai_releases.id'), nullable=False),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('native_id', sa.String(24), nullable=False), sa.Column('ready', sa.Boolean(), nullable=False))
    op.create_index('ix_ai_app_entries_release_id','ai_app_entries',['release_id'])
    op.create_table('ai_app_runs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('entry_id', sa.String(36), sa.ForeignKey('ai_app_entries.id'), nullable=False),
        sa.Column('conversation_id', sa.String(36), nullable=False), sa.Column('message_id', sa.String(100), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_ai_app_runs_conversation_id','ai_app_runs',['conversation_id'])
    op.create_table('ai_app_calls',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('ai_applications.project_id'), nullable=False),
        sa.Column('run_id', sa.String(36), sa.ForeignKey('ai_app_runs.id'), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False), sa.Column('state', sa.String(24), nullable=False),
        sa.Column('reserved_micros', sa.BigInteger(), nullable=False), sa.Column('held_micros', sa.BigInteger(), nullable=False),
        sa.Column('input_rate', sa.String(40), nullable=False), sa.Column('output_rate', sa.String(40), nullable=False),
        sa.Column('prompt_tokens', sa.BigInteger()), sa.Column('completion_tokens', sa.BigInteger()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint('run_id','request_hash'))
    for column in ('project_id','run_id'):
        op.create_index('ix_ai_app_calls_'+column, 'ai_app_calls', [column])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM ai_releases')).scalar():
        raise RuntimeError('Preserve release, member and billing evidence before downgrade')
    for name in ('ai_app_calls','ai_app_runs','ai_app_entries','ai_app_members','ai_releases','ai_applications'):
        op.drop_table(name)
