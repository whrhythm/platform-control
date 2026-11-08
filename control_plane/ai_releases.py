"""Fixed AI releases, explicit members and server-only runtime admissions."""
from datetime import timezone
from uuid import UUID, NAMESPACE_URL, uuid5
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from .accounts import allowed
from .agent_tasks import seal, unseal
from .ai_trials import Owner, Resolve, draft_snapshot, usable
from .auth import Principal
from .database import get_db
from .models import (AIApplication, AIRelease, AIAppMember, AIAppEntry, AIAppRun, AIAppCall,
    AIProjectBinding, AIProjectTrial, UserEnvironment, AuditEvent, WorkspaceProject, now)
from .settings import get_settings
from .workspace_gateway import own_environment


class Publish(Owner):
    request_id: UUID
    trial_id: UUID
    expected_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    budget_micros: int = Field(ge=1000, le=100000000)
    confirm_sponsor: bool


class AccessUpdate(Owner):
    expected_revision: int = Field(ge=1)
    enabled: bool
    budget_micros: int = Field(ge=1000, le=100000000)


class Member(Owner):
    # Resolved from the registered, verified OpenID account by the trusted BFF.
    subject: str = Field(min_length=1, max_length=255)
    native_id: str = Field(pattern=r'^[0-9a-f]{24}$')
    label: str = Field(min_length=1, max_length=255)
    enabled: bool


class Search(Owner):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=2000)


class Revoke(Owner):
    native_id: str = Field(pattern=r'^[0-9a-f]{24}$')


def enabled():
    settings = get_settings()
    if not settings.ai_releases_enabled:
        raise HTTPException(404, 'ai_releases_disabled')
    return settings


def environment(db, principal):
    env = own_environment(db, principal)
    if env.status in ('deleted', 'suspended') or not allowed(db, env.issuer, env.subject):
        raise HTTPException(403, 'ai_releases_access_denied')
    return env


def owner(db, principal, project_id, native_id=None):
    env = environment(db, principal)
    db.refresh(env, with_for_update=True)
    binding = db.get(AIProjectBinding, str(project_id))
    if (not binding or binding.environment_id != env.id or binding.state != 'ready' or
            (native_id and binding.native_owner_id != native_id)):
        raise HTTPException(404, 'ai_releases_not_found')
    return env, binding


def check_access(db, principal, project_id, native_id=None):
    settings = enabled()
    actor = environment(db, principal)
    app = db.get(AIApplication, str(project_id))
    binding = db.get(AIProjectBinding, str(project_id))
    sponsor = db.get(UserEnvironment, binding.environment_id) if binding else None
    if not app or not sponsor or not app.enabled or not app.active_release_id:
        raise HTTPException(403, 'ai_releases_access_denied')
    if sponsor.status in ('deleted', 'suspended') or not allowed(db, sponsor.issuer, sponsor.subject):
        raise HTTPException(403, 'ai_releases_access_denied')
    if actor.id == sponsor.id:
        expected = binding.native_owner_id
    else:
        member = db.get(AIAppMember, (app.project_id, actor.id))
        if not member or not member.enabled:
            raise HTTPException(403, 'ai_releases_access_denied')
        expected = member.native_id
    if native_id and native_id != expected:
        raise HTTPException(403, 'ai_releases_access_denied')
    return actor, sponsor, app, settings


def entry_access(db, principal, entry_id, native_id=None):
    entry = db.get(AIAppEntry, str(entry_id))
    release = db.get(AIRelease, entry.release_id) if entry else None
    if not release:
        raise HTTPException(404, 'ai_releases_not_found')
    actor, sponsor, app, settings = check_access(db, principal, release.project_id, native_id)
    if entry.environment_id != actor.id or (native_id and entry.native_id != native_id):
        raise HTTPException(403, 'ai_releases_access_denied')
    trial = db.get(AIProjectTrial, release.trial_id)
    snap = usable(db, trial, settings)
    return entry, release, trial, snap, actor, sponsor, app, settings


def audit(db, env, principal, action, identifier):
    db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
        action='ai_release.'+action, target_id=identifier))


def release_out(db, release):
    trial = db.get(AIProjectTrial, release.trial_id)
    return {'id': release.id, 'project_id': release.project_id, 'number': release.number,
        'trial_id': trial.id, 'digest': trial.digest, 'knowledge_revision_id': trial.knowledge_revision_id,
        'created_at': release.created_at.replace(tzinfo=release.created_at.tzinfo or timezone.utc)}


def entry_out(db, entry, release):
    project = db.get(WorkspaceProject, release.project_id)
    from .ai_application_contracts import AgentSnapshot
    snap = AgentSnapshot.model_validate(unseal(db.get(AIProjectTrial, release.trial_id).encrypted_snapshot, get_settings()))
    return {**release_out(db, release), 'entry_id': entry.id, 'agent_id': 'agent_app_'+UUID(entry.id).hex,
        'name': project.name, 'model': snap.model.model, 'ready': entry.ready, 'payer': 'creator'}


def detail(db, project_id, settings):
    app = db.get(AIApplication, str(project_id))
    if not app:
        return {'application': None, 'releases': [], 'members': []}
    calls = db.execute(select(AIAppCall.state, func.count(), func.sum(AIAppCall.held_micros))
        .where(AIAppCall.project_id == app.project_id).group_by(AIAppCall.state)).all()
    return {'application': {'project_id': app.project_id, 'enabled': app.enabled,
        'active_release_id': app.active_release_id, 'revision': app.revision, 'budget_micros': app.budget_micros,
        'held_micros': sum(v[2] or 0 for v in calls), 'calls': [{'state': v[0], 'count': v[1]} for v in calls]},
        'releases': [release_out(db, r) for r in db.scalars(select(AIRelease).where(
            AIRelease.project_id == app.project_id).order_by(AIRelease.number.desc()))],
        'members': [{'native_id': m.native_id, 'label': unseal(m.encrypted_label, settings), 'enabled': m.enabled}
            for m in db.scalars(select(AIAppMember).where(AIAppMember.project_id == app.project_id))]}


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/ai-projects', tags=['ai-releases'])

    @router.get('/{project_id}/releases')
    def listing(project_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = enabled()
        owner(db, principal, project_id)
        return detail(db, project_id, settings)

    @router.post('/{project_id}/releases')
    def publish(project_id: UUID, body: Publish, principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = enabled()
        env, binding = owner(db, principal, project_id, body.native_owner_id)
        identifier = str(uuid5(NAMESPACE_URL, binding.project_id+'/release/'+str(body.request_id)))
        prior = db.get(AIRelease, identifier)
        if prior:
            trial = db.get(AIProjectTrial, prior.trial_id)
            if prior.trial_id != str(body.trial_id) or trial.digest != body.expected_digest:
                raise HTTPException(409, 'ai_releases_request_conflict')
            return detail(db, project_id, settings)
        if not body.confirm_sponsor:
            raise HTTPException(409, 'ai_releases_confirmation_required')
        trial = db.get(AIProjectTrial, str(body.trial_id))
        if not trial or trial.project_id != binding.project_id or trial.state != 'ready':
            raise HTTPException(409, 'ai_releases_trial_required')
        usable(db, trial, settings)
        accepted = unseal(trial.encrypted_acceptance, settings) if trial.encrypted_acceptance else {}
        _, _, current = draft_snapshot(db, binding, settings)
        if (accepted.get('outcome') != 'passed' or current.digest() != trial.digest or
                trial.digest != body.expected_digest):
            raise HTTPException(409, 'ai_releases_trial_required')
        app = db.get(AIApplication, binding.project_id)
        if not app:
            app = AIApplication(project_id=binding.project_id, budget_micros=body.budget_micros)
            db.add(app)
            db.flush()
        count = db.scalar(select(func.count()).select_from(AIRelease).where(AIRelease.project_id == binding.project_id))
        if count >= settings.ai_releases_limit:
            raise HTTPException(409, 'ai_releases_limit')
        release = AIRelease(id=identifier, project_id=binding.project_id, trial_id=trial.id,
            request_id=str(body.request_id), number=count+1)
        db.add(release)
        app.active_release_id, app.enabled = identifier, True
        # Publishing never resets accumulated spend or silently changes the existing budget.
        app.revision += 1
        audit(db, env, principal, 'published', identifier)
        db.commit()
        return detail(db, project_id, settings)

    @router.post('/{project_id}/access')
    def access(project_id: UUID, body: AccessUpdate, principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = enabled()
        env, _ = owner(db, principal, project_id, body.native_owner_id)
        app = db.get(AIApplication, str(project_id))
        if not app or app.revision != body.expected_revision:
            raise HTTPException(409, 'ai_releases_changed')
        app.enabled, app.budget_micros = body.enabled, body.budget_micros
        app.revision += 1
        audit(db, env, principal, 'access_updated', app.project_id)
        db.commit()
        return detail(db, project_id, settings)

    @router.post('/{project_id}/members')
    def member(project_id: UUID, body: Member, principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = enabled()
        env, _ = owner(db, principal, project_id, body.native_owner_id)
        target = db.scalar(select(UserEnvironment).where(UserEnvironment.issuer == principal.issuer,
            UserEnvironment.subject == body.subject))
        if not db.get(AIApplication, str(project_id)) or not target or target.id == env.id:
            raise HTTPException(409, 'ai_releases_member_unavailable')
        row = db.get(AIAppMember, (str(project_id), target.id))
        if not row:
            if db.scalar(select(func.count()).select_from(AIAppMember).where(
                    AIAppMember.project_id == str(project_id))) >= settings.ai_releases_member_limit:
                raise HTTPException(409, 'ai_releases_limit')
            row = AIAppMember(project_id=str(project_id), environment_id=target.id,
                native_id=body.native_id, encrypted_label=seal(body.label, settings))
            db.add(row)
        if row.native_id != body.native_id:
            raise HTTPException(409, 'ai_releases_member_unavailable')
        row.enabled = body.enabled
        audit(db, env, principal, 'member_enabled' if body.enabled else 'member_revoked', body.native_id)
        db.commit()
        return detail(db, project_id, settings)

    @router.post('/{project_id}/members/revoke')
    def revoke(project_id: UUID, body: Revoke, principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = enabled()
        env, _ = owner(db, principal, project_id, body.native_owner_id)
        row = db.scalar(select(AIAppMember).where(AIAppMember.project_id == str(project_id),
            AIAppMember.native_id == body.native_id))
        if not row:
            raise HTTPException(404, 'ai_releases_member_unavailable')
        row.enabled = False
        audit(db, env, principal, 'member_revoked', body.native_id)
        db.commit()
        return detail(db, project_id, settings)

    @router.post('/{project_id}/launch')
    def launch(project_id: UUID, body: Owner, principal=Depends(principal_dependency), db=Depends(get_db)):
        actor, sponsor, app, settings = check_access(db, principal, project_id, body.native_owner_id)
        db.refresh(sponsor, with_for_update=True)
        release = db.get(AIRelease, app.active_release_id)
        usable(db, db.get(AIProjectTrial, release.trial_id), settings)
        identifier = str(uuid5(NAMESPACE_URL, release.id+'/'+actor.id))
        entry = db.get(AIAppEntry, identifier)
        if not entry:
            entry = AIAppEntry(id=identifier, release_id=release.id, environment_id=actor.id, native_id=body.native_owner_id)
            db.add(entry)
            audit(db, sponsor, principal, 'entry_prepared', identifier)
            db.commit()
        return entry_out(db, entry, release)

    @router.post('/entries/{entry_id}/confirm')
    def confirm(entry_id: UUID, body: Owner, principal=Depends(principal_dependency), db=Depends(get_db)):
        entry, release, _, _, _, sponsor, _, _ = entry_access(db, principal, entry_id, body.native_owner_id)
        if not entry.ready:
            entry.ready = True
            audit(db, sponsor, principal, 'entry_ready', entry.id)
            db.commit()
        return entry_out(db, entry, release)

    @router.post('/entries/{entry_id}/resolve')
    def resolve(entry_id: UUID, body: Resolve, principal=Depends(principal_dependency), db=Depends(get_db)):
        entry, release, trial, snap, actor, sponsor, _, settings = entry_access(db, principal, entry_id, body.native_owner_id)
        if not entry.ready:
            raise HTTPException(409, 'ai_releases_not_ready')
        db.refresh(sponsor, with_for_update=True)
        previous = db.scalar(select(AIAppRun).where(AIAppRun.conversation_id == str(body.conversation_id)))
        if previous and previous.entry_id != entry.id:
            raise HTTPException(409, 'ai_releases_session_mismatch')
        identifier = str(uuid5(NAMESPACE_URL, entry.id+'/'+body.message_id))
        run = db.get(AIAppRun, identifier)
        if run and run.conversation_id != str(body.conversation_id):
            raise HTTPException(409, 'ai_releases_session_mismatch')
        if not run:
            run = AIAppRun(id=identifier, entry_id=entry.id, conversation_id=str(body.conversation_id), message_id=body.message_id)
            db.add(run)
            audit(db, sponsor, principal, 'run_admitted', identifier)
            db.commit()
        expiry = int(run.created_at.replace(tzinfo=run.created_at.tzinfo or timezone.utc).timestamp())+settings.ai_releases_run_seconds
        token = jwt.encode({'aud':'ai-application', 'sub':actor.subject, 'iss':actor.issuer,
            'run':run.id, 'entry':entry.id, 'exp':expiry}, settings.librechat_service_token, algorithm='HS256')
        return {**entry_out(db, entry, release), 'snapshot':snap.model_dump(mode='json'),
            'knowledge_revision_id':trial.knowledge_revision_id, 'credential':'sbapp_'+token}

    @router.post('/entries/{entry_id}/search')
    def search(entry_id: UUID, body: Search, principal=Depends(principal_dependency), db=Depends(get_db)):
        _, release, trial, _, _, _, _, settings = entry_access(db, principal, entry_id)
        if not trial.knowledge_revision_id:
            return {'revision_id':None, 'sources':[]}
        from .knowledge import KnowledgeClient, revision_scope
        from .models import ProjectKnowledge
        revision = trial.knowledge_revision_id
        row = db.get(ProjectKnowledge, revision)
        if row.chunks == 0:
            return {'revision_id': revision, 'sources': []}
        scope = revision_scope(row)
        db.commit()
        try:
            result = KnowledgeClient(settings).call('POST', scope, revision, {'query':body.query,'k':5}, query=True)
        except Exception:
            raise HTTPException(503, 'ai_releases_knowledge_unavailable') from None
        db.expire_all()
        entry_access(db, principal, entry_id)
        return {'revision_id':revision, 'sources':result['sources']}

    @router.get('/entries/{entry_id}/source')
    def source(entry_id: UUID, name: str = Query(min_length=1,max_length=124), principal=Depends(principal_dependency), db=Depends(get_db)):
        _, _, trial, _, _, _, _, settings = entry_access(db, principal, entry_id)
        from .models import ProjectKnowledge
        revision = db.get(ProjectKnowledge, trial.knowledge_revision_id) if trial.knowledge_revision_id else None
        for item in unseal(revision.encrypted_source, settings) if revision else []:
            if item['name'] == name:
                return {**item, 'revision_id':revision.id, 'digest':revision.digest}
        raise HTTPException(404, 'ai_releases_not_found')

    return router
