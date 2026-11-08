"""Current grants and cumulative member accounting, never snapshots of authority."""
from datetime import datetime, timezone
from uuid import UUID
from fastapi import Depends, HTTPException
from pydantic import Field
from sqlalchemy import func, select
from .ai_trials import Owner
from .accounts import allowed
from .database import get_db
from .models import (AIApplication, AIAppMember, AIAppEntry, AIAppRun, AIAppCall,
                     AIProjectBinding, AIRelease, UserEnvironment, WorkspaceProject, now)


def utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else None


def member_state(member):
    if not member.enabled:
        return member.state if member.state == 'declined' else 'revoked'
    if member.expires_at and utc(member.expires_at) <= now():
        return 'expired'
    return member.state


def usage(db, project_id, environment_id=None):
    query = select(AIAppCall.state, func.coalesce(func.sum(AIAppCall.held_micros), 0)).where(
        AIAppCall.project_id == project_id)
    if environment_id:
        query = query.join(AIAppRun, AIAppRun.id == AIAppCall.run_id).join(
            AIAppEntry, AIAppEntry.id == AIAppRun.entry_id).where(AIAppEntry.environment_id == environment_id)
    amounts = dict(db.execute(query.group_by(AIAppCall.state)).all())
    return {'spent_micros': amounts.get('completed', 0),
            'pending_micros': sum(v for k, v in amounts.items() if k != 'completed'),
            'held_micros': sum(amounts.values())}


def member_out(db, member, settings):
    from .agent_tasks import unseal
    return {'native_id': member.native_id, 'label': unseal(member.encrypted_label, settings),
            'enabled': member.enabled, 'state': member_state(member), 'revision': member.revision,
            'budget_micros': member.budget_micros, 'expires_at': utc(member.expires_at),
            **usage(db, member.project_id, member.environment_id)}


class MemberPolicy(Owner):
    native_id: str = Field(pattern=r'^[0-9a-f]{24}$')
    expected_revision: int = Field(ge=1)
    budget_micros: int | None = Field(default=None, ge=0, le=100000000)
    expires_at: datetime | None = None


class InvitationReply(Owner):
    expected_revision: int = Field(ge=1)
    accept: bool


def add_routes(router, principal_dependency):
    from .ai_releases import environment, enabled, owner, audit, detail

    @router.get('/catalog')
    def catalog(principal=Depends(principal_dependency), db=Depends(get_db)):
        enabled()
        actor = environment(db, principal)
        grants = {m.project_id: m for m in db.scalars(select(AIAppMember).where(
            AIAppMember.environment_id == actor.id))}
        bindings = list(db.scalars(select(AIProjectBinding).where(
            (AIProjectBinding.environment_id == actor.id) | AIProjectBinding.project_id.in_(grants))))
        items, invitations = [], []
        for binding in bindings:
            app = db.get(AIApplication, binding.project_id)
            release = db.get(AIRelease, app.active_release_id) if app and app.enabled and app.active_release_id else None
            if not release:
                continue
            sponsor = db.get(UserEnvironment, binding.environment_id)
            if sponsor.status in ('deleted', 'suspended') or not allowed(db, sponsor.issuer, sponsor.subject):
                continue
            mine = sponsor.id == actor.id
            member = grants.get(binding.project_id)
            state = 'active' if mine else member_state(member)
            if state not in ('active', 'pending'):
                continue
            item = {'project_id': binding.project_id, 'release_id': release.id, 'number': release.number,
                    'name': release.name or db.get(WorkspaceProject, binding.project_id).name,
                    'kind': 'mine' if mine else 'shared', 'state': state, 'payer': 'creator',
                    'revision': app.revision if mine else member.revision,
                    'expires_at': None if mine else utc(member.expires_at),
                    'budget_micros': app.budget_micros if mine or member.budget_micros is None else member.budget_micros,
                    'application_budget_micros': app.budget_micros,
                    'application_held_micros': usage(db, app.project_id)['held_micros'],
                    **usage(db, app.project_id, None if mine else actor.id)}
            (invitations if state == 'pending' else items).append(item)
        return {'items': items, 'invitations': invitations}

    @router.post('/{project_id}/invitation')
    def respond(project_id: UUID, body: InvitationReply, principal=Depends(principal_dependency), db=Depends(get_db)):
        enabled()
        actor = environment(db, principal)
        binding = db.get(AIProjectBinding, str(project_id))
        sponsor = db.get(UserEnvironment, binding.environment_id) if binding else None
        if not sponsor:
            raise HTTPException(404, 'ai_releases_not_found')
        db.refresh(sponsor, with_for_update=True)
        member = db.get(AIAppMember, (str(project_id), actor.id))
        app = db.get(AIApplication, str(project_id))
        if (not member or member.native_id != body.native_owner_id or not app or not app.enabled
                or not app.active_release_id or sponsor.status in ('deleted', 'suspended')
                or not allowed(db, sponsor.issuer, sponsor.subject)):
            raise HTTPException(403, 'ai_releases_access_denied')
        if member.revision != body.expected_revision:
            raise HTTPException(409, 'ai_releases_changed')
        desired = 'active' if body.accept else 'declined'
        if member_state(member) == desired:
            return {'state': desired}
        if member_state(member) != 'pending':
            raise HTTPException(409, 'ai_releases_invitation_unavailable')
        member.state, member.enabled = desired, body.accept
        audit(db, sponsor, principal, 'invitation_'+desired, member.project_id+'/'+member.native_id+'@'+str(member.revision))
        db.commit()
        return {'state': desired}

    @router.post('/{project_id}/members/policy')
    def policy(project_id: UUID, body: MemberPolicy, principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = enabled()
        env, _ = owner(db, principal, project_id, body.native_owner_id)
        member = db.scalar(select(AIAppMember).where(AIAppMember.project_id == str(project_id),
            AIAppMember.native_id == body.native_id))
        if not member:
            raise HTTPException(404, 'ai_releases_member_unavailable')
        if member.revision != body.expected_revision:
            raise HTTPException(409, 'ai_releases_changed')
        if body.expires_at and utc(body.expires_at) <= now():
            raise HTTPException(422, 'ai_releases_expiry_invalid')
        member.budget_micros, member.expires_at = body.budget_micros, utc(body.expires_at)
        member.revision += 1
        audit(db, env, principal, 'member_policy_updated', member.project_id+'/'+member.native_id+
            '@'+str(member.revision)+':budget='+str(member.budget_micros)+':expires='+str(member.expires_at))
        db.commit()
        return detail(db, project_id, settings)
