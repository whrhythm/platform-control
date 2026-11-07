"""Owner-only fixed previews; the authenticated BFF supplies native chat receipts."""
from hashlib import sha256
from datetime import timezone
from typing import Literal
from uuid import UUID, NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .agent_tasks import seal, unseal
from .ai_application_contracts import AgentSnapshot, ModelRevision, KnowledgeRevision, SkillRevision, ToolRevision
from .ai_policy import effective_policy
from .database import get_db
from .models import AIProjectBinding, AIProjectDraft, AIProjectTrial, AITrialSession, AITrialRun, AuditEvent, ProjectKnowledge, WorkspaceProject, now
from .settings import get_settings
from .workspace_gateway import own_environment


STYLES = {
    'grounded': '先直接回答问题，再列出依据。资料没有提供的内容明确说明无法确认，不编造事实或引用。',
    'support': '按“结论、依据、下一步”三个简短段落回答；无法从资料确认的结论明确说明，必要时提出一个澄清问题。',
}


class Config(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    instructions: str = Field(min_length=1, max_length=32000)
    model: str = Field(min_length=1, max_length=200)
    temperature: float = Field(default=0, ge=0, le=2, allow_inf_nan=False)
    max_output_tokens: int = Field(default=2048, ge=256, le=16384)
    response_style: Literal['grounded', 'support'] = 'grounded'


class Owner(BaseModel):
    model_config = ConfigDict(extra='forbid')
    native_owner_id: str = Field(pattern=r'^[0-9a-f]{24}$')


class Initialize(Owner):
    config: Config


class Update(Initialize):
    expected_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class Create(Owner):
    request_id: UUID
    expected_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class Resolve(Owner):
    conversation_id: UUID
    message_id: str = Field(min_length=1, max_length=100)


class Acceptance(Resolve):
    outcome: Literal['passed', 'failed']
    note: str = Field(min_length=1, max_length=2000)
    message_id: str = Field(min_length=1, max_length=100)
    transcript_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    # These assertions are manually confirmed in the UI. They are not model grades.
    answer_checked: bool
    sources_checked: bool


def trial_agent_id(identifier):
    return 'agent_trial_' + UUID(identifier).hex


def snapshot(db, binding, config, settings):
    revision = db.get(ProjectKnowledge, binding.knowledge_revision_id) if binding.knowledge_revision_id else None
    if binding.knowledge_revision_id and (not settings.knowledge_enabled or not revision or revision.state != 'ready'):
        raise HTTPException(409, 'ai_trials_knowledge_unavailable')
    return AgentSnapshot(instructions=config.instructions,
        model=ModelRevision(provider='LiteLLM', model=config.model,
            # This identifies the configured gateway route, not an immutable provider model build.
            route_revision=sha256(settings.litellm_url.encode()).hexdigest(),
            temperature=config.temperature, max_output_tokens=config.max_output_tokens),
        skills=(SkillRevision(skill_id=config.response_style, revision='1', content=STYLES[config.response_style]),),
        knowledge=(KnowledgeRevision(collection_id=binding.project_id,
            file_id='collection:'+revision.id, file_digest=revision.digest,
            index_revision=revision.id, index_digest=revision.digest, grant_id=binding.project_id),) if revision else (),
        tools=(ToolRevision(tool_id='file_search', definition_digest=sha256(b'project-file-search-v1').hexdigest(),
            grant_id=binding.project_id),) if revision else ())


def config_allowed(db, config, settings):
    if config.model not in effective_policy(db, settings)['allowed_models']:
        raise HTTPException(409, 'ai_trials_model_unavailable')
    if config.max_output_tokens > settings.ai_trials_output_limit:
        raise HTTPException(409, 'ai_trials_output_limit')


def draft_snapshot(db, binding, settings):
    draft = db.get(AIProjectDraft, binding.project_id)
    if not draft:
        raise HTTPException(409, 'ai_trials_draft_required')
    config = Config.model_validate(unseal(draft.encrypted_config, settings))
    return draft, config, snapshot(db, binding, config, settings)


def usable(db, row, settings):
    snap = AgentSnapshot.model_validate(unseal(row.encrypted_snapshot, settings))
    if (snap.model.model not in effective_policy(db, settings)['allowed_models'] or
            snap.model.route_revision != sha256(settings.litellm_url.encode()).hexdigest()):
        raise HTTPException(409, 'ai_trials_model_unavailable')
    if snap.model.max_output_tokens > settings.ai_trials_output_limit:
        raise HTTPException(409, 'ai_trials_output_limit')
    if row.knowledge_revision_id:
        revision = db.get(ProjectKnowledge, row.knowledge_revision_id)
        if (not settings.knowledge_enabled or not revision or revision.project_id != row.project_id or
                revision.environment_id != row.environment_id or revision.state != 'ready' or
                revision.digest != snap.knowledge[0].index_digest):
            raise HTTPException(409, 'ai_trials_knowledge_unavailable')
    return snap


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/ai-projects', tags=['ai-trials'])

    def owned(db, principal, project_id, owner=None):
        settings = get_settings()
        if not settings.ai_projects_enabled or not settings.ai_trials_enabled:
            raise HTTPException(404, 'ai_trials_disabled')
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        if env.status == 'suspended':
            raise HTTPException(403, 'ai_trials_account_suspended')
        binding = db.get(AIProjectBinding, str(project_id))
        if not binding or binding.environment_id != env.id or binding.state != 'ready':
            raise HTTPException(404, 'ai_trials_not_found')
        if owner and owner != binding.native_owner_id:
            raise HTTPException(403, 'ai_trials_owner_mismatch')
        return env, binding, settings

    def audit(db, env, principal, action, identifier):
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer,
            subject=principal.subject, action='ai_trial.'+action, target_id=identifier))

    def trial_out(row, settings, current_digest=None):
        accepted = unseal(row.encrypted_acceptance, settings) if row.encrypted_acceptance else None
        snap = AgentSnapshot.model_validate(unseal(row.encrypted_snapshot, settings))
        return {'id': row.id, 'project_id': row.project_id, 'request_id': row.request_id,
            'number': row.number, 'agent_id': trial_agent_id(row.id), 'digest': row.digest,
            'state': row.state, 'knowledge_revision_id': row.knowledge_revision_id,
            'model': snap.model.model, 'created_at': row.created_at.replace(tzinfo=row.created_at.tzinfo or timezone.utc), 'acceptance': accepted,
            'acceptance_valid': bool(accepted and accepted['outcome'] == 'passed' and row.digest == current_digest)}

    def detail(db, binding, settings):
        draft = db.get(AIProjectDraft, binding.project_id)
        current = None
        config = None
        error = None
        if draft:
            config = Config.model_validate(unseal(draft.encrypted_config, settings))
            try:
                current = snapshot(db, binding, config, settings).digest()
                config_allowed(db, config, settings)
            except HTTPException as problem:
                error = problem.detail
        rows = db.scalars(select(AIProjectTrial).where(AIProjectTrial.project_id == binding.project_id)
            .order_by(AIProjectTrial.number.desc()))
        return {'project_id': binding.project_id, 'revision': draft.revision if draft else None,
            'config': config.model_dump() if config else None, 'digest': current,
            'knowledge_revision_id': binding.knowledge_revision_id, 'unavailable_reason': error,
            'models': effective_policy(db, settings)['allowed_models'], 'output_limit': settings.ai_trials_output_limit,
            'trials': [{**trial_out(row, settings, current if not error else None), 'conversation_ids': list(db.scalars(
                select(AITrialSession.conversation_id).where(AITrialSession.trial_id == row.id)
                .order_by(AITrialSession.created_at.desc())))} for row in rows]}

    @router.get('/{project_id}/trials')
    def listing(project_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        _, binding, settings = owned(db, principal, project_id)
        return detail(db, binding, settings)

    @router.post('/{project_id}/draft/initialize')
    def initialize(project_id: UUID, body: Initialize, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, binding, settings = owned(db, principal, project_id, body.native_owner_id)
        if not db.get(AIProjectDraft, binding.project_id):
            config_allowed(db, body.config, settings)
            db.add(AIProjectDraft(project_id=binding.project_id, encrypted_config=seal(body.config.model_dump(), settings)))
            audit(db, env, principal, 'draft_initialized', binding.project_id)
            db.commit()
        return detail(db, binding, settings)

    @router.post('/{project_id}/draft')
    def update(project_id: UUID, body: Update, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, binding, settings = owned(db, principal, project_id, body.native_owner_id)
        draft, _, current = draft_snapshot(db, binding, settings)
        if current.digest() != body.expected_digest:
            raise HTTPException(409, 'ai_trials_draft_changed')
        config_allowed(db, body.config, settings)
        if snapshot(db, binding, body.config, settings).digest() != current.digest():
            draft.encrypted_config = seal(body.config.model_dump(), settings)
            draft.revision += 1
            draft.updated_at = now()
            audit(db, env, principal, 'draft_updated', binding.project_id)
            db.commit()
        return detail(db, binding, settings)

    @router.post('/{project_id}/trials')
    def create(project_id: UUID, body: Create, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, binding, settings = owned(db, principal, project_id, body.native_owner_id)
        identifier = str(uuid5(NAMESPACE_URL, binding.project_id+'/trial/'+str(body.request_id)))
        row = db.get(AIProjectTrial, identifier)
        if row:
            if row.digest != body.expected_digest:
                raise HTTPException(409, 'ai_trials_request_conflict')
            return trial_out(row, settings)
        _, config, current = draft_snapshot(db, binding, settings)
        if current.digest() != body.expected_digest:
            raise HTTPException(409, 'ai_trials_draft_changed')
        config_allowed(db, config, settings)
        count = db.scalar(select(func.count()).select_from(AIProjectTrial).where(AIProjectTrial.project_id == binding.project_id))
        if count >= settings.ai_trials_limit:
            raise HTTPException(409, 'ai_trials_limit')
        row = AIProjectTrial(id=identifier, project_id=binding.project_id, environment_id=env.id,
            request_id=str(body.request_id), number=count+1, digest=current.digest(),
            encrypted_snapshot=seal(current.model_dump(mode='json'), settings),
            knowledge_revision_id=binding.knowledge_revision_id)
        db.add(row)
        audit(db, env, principal, 'requested', identifier)
        db.commit()
        return trial_out(row, settings)

    def trial_owned(db, principal, project_id, trial_id, owner):
        env, binding, settings = owned(db, principal, project_id, owner)
        row = db.get(AIProjectTrial, str(trial_id))
        if not row or row.project_id != binding.project_id or row.environment_id != env.id:
            raise HTTPException(404, 'ai_trials_not_found')
        return env, binding, settings, row

    @router.post('/{project_id}/trials/{trial_id}/prepare')
    def prepare(project_id: UUID, trial_id: UUID, body: Owner, principal=Depends(principal_dependency), db=Depends(get_db)):
        _, _, settings, row = trial_owned(db, principal, project_id, trial_id, body.native_owner_id)
        snap = usable(db, row, settings)
        project = db.get(WorkspaceProject, row.project_id)
        return {**trial_out(row, settings), 'initial_agent': {'name': project.name[:60]+' · 试用 '+str(row.number),
            'description': '固定配置试用；配置修改后请回项目创建新预览。',
            'instructions': snap.instructions, 'provider': snap.model.provider, 'model': snap.model.model}}

    @router.post('/{project_id}/trials/{trial_id}/confirm')
    def confirm(project_id: UUID, trial_id: UUID, body: Owner, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, _, settings, row = trial_owned(db, principal, project_id, trial_id, body.native_owner_id)
        usable(db, row, settings)
        if row.state != 'ready':
            row.state = 'ready'
            audit(db, env, principal, 'ready', row.id)
            db.commit()
        return trial_out(row, settings)

    @router.post('/{project_id}/trials/{trial_id}/resolve')
    def resolve(project_id: UUID, trial_id: UUID, body: Resolve, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, binding, settings, row = trial_owned(db, principal, project_id, trial_id, body.native_owner_id)
        snap = usable(db, row, settings)
        if row.state != 'ready':
            raise HTTPException(409, 'ai_trials_not_ready')
        session = db.get(AITrialSession, str(body.conversation_id))
        if session and (session.environment_id != env.id or session.trial_id != row.id):
            raise HTTPException(409, 'ai_trials_session_mismatch')
        if not session:
            db.add(AITrialSession(conversation_id=str(body.conversation_id), trial_id=row.id, environment_id=env.id))
            db.flush()
            audit(db, env, principal, 'session_bound', row.id)
        run = db.get(AITrialRun, body.message_id)
        if run and (run.conversation_id != str(body.conversation_id) or run.trial_id != row.id):
            raise HTTPException(409, 'ai_trials_session_mismatch')
        if not run:
            db.add(AITrialRun(message_id=body.message_id, conversation_id=str(body.conversation_id), trial_id=row.id))
        db.commit()
        return {**trial_out(row, settings), 'snapshot': snap.model_dump(mode='json')}

    @router.post('/trials/{trial_id}/resolve')
    def resolve_by_id(trial_id: UUID, body: Resolve, principal=Depends(principal_dependency), db=Depends(get_db)):
        row = db.get(AIProjectTrial, str(trial_id))
        if not row:
            raise HTTPException(404, 'ai_trials_not_found')
        return resolve(UUID(row.project_id), trial_id, body, principal, db)

    @router.post('/{project_id}/trials/{trial_id}/acceptance')
    def accept(project_id: UUID, trial_id: UUID, body: Acceptance, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, binding, settings, row = trial_owned(db, principal, project_id, trial_id, body.native_owner_id)
        usable(db, row, settings)
        session = db.get(AITrialSession, str(body.conversation_id))
        if not session or session.trial_id != row.id or session.environment_id != env.id or row.state != 'ready':
            raise HTTPException(409, 'ai_trials_session_mismatch')
        run = db.get(AITrialRun, body.message_id)
        if not run or run.trial_id != row.id or run.conversation_id != session.conversation_id:
            raise HTTPException(409, 'ai_trials_answer_required')
        _, _, current = draft_snapshot(db, binding, settings)
        if body.outcome == 'passed' and (current.digest() != row.digest or not body.answer_checked or not body.sources_checked):
            raise HTTPException(409, 'ai_trials_confirmation_required')
        receipt = {**body.model_dump(mode='json', exclude={'native_owner_id'}), 'recorded_at': now().isoformat(), 'digest': row.digest}
        # One immutable decision per preview: further tests use a new preview instead of overwriting evidence.
        if row.encrypted_acceptance:
            existing = unseal(row.encrypted_acceptance, settings)
            if any(existing.get(k) != v for k, v in receipt.items() if k != 'recorded_at'):
                raise HTTPException(409, 'ai_trials_acceptance_exists')
        else:
            row.encrypted_acceptance = seal(receipt, settings)
            audit(db, env, principal, 'accepted_'+body.outcome, row.id)
            db.commit()
        return trial_out(row, settings, current.digest())

    return router
