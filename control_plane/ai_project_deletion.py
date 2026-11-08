"""Owner-confirmed retirement. The knowledge worker owns physical index cleanup."""
from hashlib import sha256
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import delete, or_, select, update

from .agent_tasks import ACTIVE, seal
from .ai_projects import Owner
from .database import get_db
from .models import (AIAppEntry, AIAppMember, AIApplication, AIProjectBinding, AIProjectDeletion,
    AIProjectDraft, AIProjectTrial, AIRelease, AITrialRun, AITrialSession, AuditEvent,
    AgentTask, KnowledgeLibrary, ProjectDatabase, ProjectKnowledge, UserApplication, WorkspaceProject, now)
from .settings import get_settings
from .workspace_gateway import call_files, own_environment


class Advance(Owner):
    confirmation: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    remove_folder: bool = False


def root_in_use(db, project):
    path = project.project_path
    env_id = project.environment_id
    return any(db.scalar(select(model.id).where(model.environment_id == env_id,
        model.project_path == path, model.status != 'deleted').limit(1))
        for model in (UserApplication, ProjectDatabase)) or bool(db.scalar(select(AgentTask.id).where(
            AgentTask.environment_id == env_id, AgentTask.status.in_(ACTIVE)).limit(1)))


def plan_folder(db, principal, project):
    if root_in_use(db, project):
        return {'reason': 'in_use'}
    try:
        scope = call_files(db, principal, get_settings(), 'GET', '/v1/project-root',
                           project.project_path, params={'project_id': project.id}).json()['scope']
    except HTTPException as error:
        if error.status_code in (400, 413):
            return {'reason': 'unverified'}
        raise HTTPException(503, 'ai_projects_folder_check_unavailable') from None
    return {'scope': scope} if scope else {'reason': 'unverified'}


def deletion_plan(db, binding, project):
    identifier = project.id
    trials = list(db.scalars(select(AIProjectTrial).where(AIProjectTrial.project_id == identifier)
                            .order_by(AIProjectTrial.id)))
    releases = select(AIRelease.id).where(AIRelease.project_id == identifier)
    entries = list(db.scalars(select(AIAppEntry).where(AIAppEntry.release_id.in_(releases))
                              .order_by(AIAppEntry.id)))
    libraries = set(db.scalars(select(ProjectKnowledge.library_id).where(
        ProjectKnowledge.id.in_([trial.knowledge_revision_id for trial in trials
                                 if trial.knowledge_revision_id]),
        ProjectKnowledge.library_id.is_not(None))))
    if binding.knowledge_library_id:
        libraries.add(binding.knowledge_library_id)
    legacy = db.get(KnowledgeLibrary, identifier)
    if legacy and legacy.environment_id == binding.environment_id:
        libraries.add(legacy.id)
    remove_legacy = bool(legacy and legacy.id in libraries and legacy.state != 'deleted' and not (
        db.scalar(select(AIProjectBinding.project_id).where(AIProjectBinding.knowledge_library_id == identifier,
            AIProjectBinding.project_id != identifier).limit(1)) or
        db.scalar(select(AIProjectTrial.id).join(ProjectKnowledge,
            AIProjectTrial.knowledge_revision_id == ProjectKnowledge.id)
            .where(ProjectKnowledge.library_id == identifier, AIProjectTrial.project_id != identifier).limit(1))))
    remove_scope = ProjectKnowledge.library_id.is_(None) & (ProjectKnowledge.project_id == identifier)
    if remove_legacy:
        remove_scope = or_(remove_scope, ProjectKnowledge.library_id == identifier)
    external_trials = select(AIProjectTrial.knowledge_revision_id).where(
        AIProjectTrial.project_id != identifier, AIProjectTrial.knowledge_revision_id.is_not(None))
    revisions = list(db.scalars(select(ProjectKnowledge.id).where(
        ProjectKnowledge.environment_id == binding.environment_id, remove_scope,
        ProjectKnowledge.state != 'deleted', ProjectKnowledge.id.not_in(external_trials))
        .order_by(ProjectKnowledge.id)))
    shared = sorted(libraries - ({identifier} if remove_legacy else set()))
    members = list(db.scalars(select(AIAppMember).where(AIAppMember.project_id == identifier)
                              .order_by(AIAppMember.environment_id)))
    return {
        'folder': project.project_path,
        'library': identifier if remove_legacy else None,
        'preserved_libraries': shared,
        'revisions': revisions,
        'trials': [trial.id for trial in trials],
        'releases': list(db.scalars(releases.order_by(AIRelease.id))),
        'members': [[row.environment_id, row.revision] for row in members if row.enabled],
        'agents': [{'id': binding.agent_id, 'owner_id': binding.native_owner_id}] +
            [{'id': 'agent_trial_' + UUID(t.id).hex, 'owner_id': binding.native_owner_id} for t in trials] +
            [{'id': 'agent_app_' + UUID(e.id).hex, 'owner_id': e.native_id} for e in entries],
    }


def preview(plan):
    folder = plan['folder_cleanup']
    scope = folder.get('scope')
    return {'state': 'confirm', 'confirmation': sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
            'folder': plan['folder'],
            'folder_cleanup': ({key: scope[key] for key in ('files', 'directories', 'bytes')}
                               if scope else None),
            'folder_retention_reason': folder.get('reason'),
            'knowledge_libraries': int(bool(plan['library'])),
            'knowledge_revisions': len(plan['revisions']), 'preserved_libraries': len(plan['preserved_libraries']),
            'trials': len(plan['trials']), 'releases': len(plan['releases']), 'shares': len(plan['members'])}


def status(db, deletion, *, include_agents=False):
    if 'revisions' not in deletion.plan:
        return {'state': 'deleted', **deletion.plan}
    rows = list(db.scalars(select(ProjectKnowledge).where(ProjectKnowledge.id.in_(deletion.plan['revisions']),
                                                         ProjectKnowledge.state != 'deleted')))
    if rows:
        return {'state': 'knowledge', 'remaining': len(rows), 'retrying': any(row.error_code for row in rows)}
    return {'state': 'native', **({'agents': deletion.plan['agents']} if include_agents else {})}


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/ai-projects', tags=['ai-project-deletion'])

    def owned(db, principal, project_id, native_owner_id=None):
        if not get_settings().ai_projects_enabled:
            raise HTTPException(404, 'ai_projects_disabled')
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        binding = db.get(AIProjectBinding, str(project_id))
        if (not binding or binding.environment_id != env.id or
                (native_owner_id and binding.native_owner_id != native_owner_id)):
            raise HTTPException(404, 'ai_projects_not_found')
        return env, binding, db.get(WorkspaceProject, binding.project_id)

    def audit(db, env, principal, action, target):
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                         action='ai_project.' + action, target_id=target))

    def plan_deletion(db, principal, binding, project):
        return {**deletion_plan(db, binding, project), 'folder_cleanup': plan_folder(db, principal, project)}

    @router.get('/{project_id}/deletion')
    def inspect(project_id: UUID, principal=Depends(principal_dependency), db=Depends(get_db)):
        _, binding, project = owned(db, principal, project_id)
        deletion = db.get(AIProjectDeletion, project.id)
        if deletion:
            return status(db, deletion)
        if binding.state != 'ready':
            raise HTTPException(409, 'ai_projects_deletion_not_ready')
        return preview(plan_deletion(db, principal, binding, project))

    @router.post('/{project_id}/deletion')
    def advance(project_id: UUID, body: Advance, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, binding, project = owned(db, principal, project_id, body.native_owner_id)
        deletion = db.get(AIProjectDeletion, project.id)
        if deletion:
            return status(db, deletion, include_agents=True)
        if binding.state != 'ready':
            raise HTTPException(409, 'ai_projects_deletion_not_ready')
        plan = plan_deletion(db, principal, binding, project)
        if (preview(plan)['confirmation'] != body.confirmation or
                bool(plan['folder_cleanup'].get('scope')) != body.remove_folder):
            raise HTTPException(409, 'ai_projects_deletion_changed')
        binding.state, binding.updated_at = 'deleting', now()
        binding.knowledge_revision_id = binding.knowledge_library_id = None
        app = db.get(AIApplication, project.id)
        if app:
            app.enabled, app.active_release_id, app.revision = False, None, app.revision + 1
        db.execute(delete(AIAppMember).where(AIAppMember.project_id == project.id))
        db.execute(update(AIProjectTrial).where(AIProjectTrial.project_id == project.id)
                   .values(state='deleted', encrypted_snapshot='', encrypted_acceptance=None,
                           knowledge_revision_id=None))
        db.execute(delete(AIProjectDraft).where(AIProjectDraft.project_id == project.id))
        if plan['library']:
            library = db.get(KnowledgeLibrary, plan['library'])
            library.state = 'deleting' if plan['revisions'] else 'deleted'
            library.head_revision_id = library.active_revision_id = None
            library.updated_at = now()
        db.execute(update(ProjectKnowledge).where(ProjectKnowledge.id.in_(plan['revisions']),
            ProjectKnowledge.state.not_in(('deleted', 'deleting')))
            .values(state='deleting', lease_token=None, lease_until=None, error_code=None, updated_at=now()))
        deletion = AIProjectDeletion(project_id=project.id, plan=plan)
        db.add(deletion)
        audit(db, env, principal, 'deletion_started', project.id)
        result = status(db, deletion, include_agents=True)
        db.commit()
        return result

    @router.post('/{project_id}/deletion/complete')
    def complete(project_id: UUID, body: Owner, principal=Depends(principal_dependency), db=Depends(get_db)):
        env, binding, project = owned(db, principal, project_id, body.native_owner_id)
        deletion = db.get(AIProjectDeletion, project.id)
        if not deletion:
            raise HTTPException(409, 'ai_projects_deletion_not_ready')
        state = status(db, deletion)['state']
        if state == 'deleted':
            return status(db, deletion)
        if state != 'native':
            raise HTTPException(409, 'ai_projects_deletion_not_ready')
        folder_retained = False
        scope = deletion.plan.get('folder_cleanup', {}).get('scope')
        if scope:
            if root_in_use(db, project):
                folder_retained = True
            else:
                try:
                    result = call_files(db, principal, get_settings(), 'DELETE', '/v1/project-root',
                        deletion.plan['folder'], params={'project_id': project.id, 'digest': scope['digest'],
                                                         'inode': scope['inode']}).json()
                except HTTPException:
                    raise HTTPException(503, 'ai_projects_folder_cleanup_unavailable') from None
                if type(result.get('removed')) is not bool:
                    raise HTTPException(503, 'ai_projects_folder_cleanup_unavailable')
                folder_retained = not result['removed']
            audit(db, env, principal, 'root_preserved' if folder_retained else 'root_removed', project.id)
        trials = select(AIProjectTrial.id).where(AIProjectTrial.project_id == project.id)
        released = select(AIRelease.trial_id).where(AIRelease.project_id == project.id)
        db.execute(delete(AITrialRun).where(AITrialRun.trial_id.in_(trials)))
        db.execute(delete(AITrialSession).where(AITrialSession.trial_id.in_(trials)))
        db.execute(delete(AIProjectTrial).where(AIProjectTrial.project_id == project.id,
                                               AIProjectTrial.id.not_in(released)))
        # Keep minimal release and actor linkage for the existing immutable billing ledger.
        db.execute(update(AIRelease).where(AIRelease.project_id == project.id).values(name=None))
        binding.encrypted_initial = ''
        binding.state, binding.updated_at = 'deleted', now()
        project.project_path, project.name = '@deleted/' + project.id, ''
        project.encrypted_goal, project.updated_at = seal('', get_settings()), now()
        deletion.plan = {'folder_retained': True} if folder_retained else {}
        audit(db, env, principal, 'deletion_completed', project.id)
        db.commit()
        return status(db, deletion)

    return router
