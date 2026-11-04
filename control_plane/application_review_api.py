"""Administrator access to the controller's bounded release review queue."""
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from .application_reviews import REVIEW_ACTIONS, REVIEW_REQUESTED, audit_target, now, public_intent
from .application_runtime import WRITE_REVIEW_CODE
from .database import get_db
from .models import AuditEvent, UserApplication, UserEnvironment


class EvidenceOut(BaseModel):
    id: UUID
    checked_at: datetime
    result: str
    actual: dict[str, Any]
    differences: list[str]


class ReviewOut(BaseModel):
    application_id: str
    project: str
    environment_id: str
    desired_state: str
    revision: str
    image: str | None
    error_code: str
    review_id: UUID | None = None
    created_at: datetime | None = None
    prior_status: str | None = None
    intent: dict[str, Any] | None = None
    evidence: EvidenceOut | None = None
    resume_pending: bool = False


class ResumeIn(BaseModel):
    review_id: UUID
    evidence_id: UUID


class ReviewAuditOut(BaseModel):
    id: str
    target_id: str
    review_id: str | None
    issuer: str
    subject: str
    action: str
    created_at: datetime


def review_out(app):
    review = app.resources.get('write_review') if app.error_code == WRITE_REVIEW_CODE else None
    return ReviewOut(
        application_id=app.id, project=app.project_path, environment_id=app.environment_id,
        desired_state=app.desired_state, revision=app.revision, image=app.image, error_code=app.error_code,
        review_id=review['id'] if review else None, created_at=review['created_at'] if review else None,
        prior_status=review['prior_status'] if review else None,
        intent=public_intent(review['intent']) if review else None,
        evidence=review.get('evidence') if review else None, resume_pending=bool(review and review.get('request')),
    )


def create_review_router(admin_dependency):
    router = APIRouter(prefix='/v1/admin/release-reviews', tags=['administration'])

    @router.get('', response_model=list[ReviewOut])
    def list_reviews(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                     _principal=Depends(admin_dependency), db=Depends(get_db)):
        apps = db.scalars(select(UserApplication).where(UserApplication.status == 'failed',
            UserApplication.error_code.endswith('_requires_review', autoescape=True))
            .order_by(UserApplication.created_at.desc(), UserApplication.id).limit(limit).offset(offset))
        return [review_out(app) for app in apps]

    @router.get('/audit', response_model=list[ReviewAuditOut])
    def list_audit(limit: int = Query(25, ge=1, le=100), _principal=Depends(admin_dependency), db=Depends(get_db)):
        events = db.scalars(select(AuditEvent).where(AuditEvent.action.in_(REVIEW_ACTIONS))
                            .order_by(AuditEvent.created_at.desc(), AuditEvent.id).limit(limit))
        result = []
        for event in events:
            app_id, _, review_id = event.target_id.partition('/')
            result.append(ReviewAuditOut(id=event.id, target_id=app_id, review_id=review_id or None,
                                         issuer=event.issuer, subject=event.subject,
                                         action=event.action, created_at=event.created_at))
        return result

    @router.post('/{application_id}/resume', status_code=202, response_model=ReviewOut,
                 responses={404: {'description': 'Application not found'},
                            409: {'description': 'Review changed or the evidence does not support resuming'}})
    def request_resume(application_id: UUID, body: ResumeIn, principal=Depends(admin_dependency), db=Depends(get_db)):
        app = db.get(UserApplication, str(application_id), with_for_update=True)
        if not app:
            raise HTTPException(404, 'Application not found')
        review = app.resources.get('write_review')
        if (app.status != 'failed' or app.error_code != WRITE_REVIEW_CODE or not review or
                review['id'] != str(body.review_id) or review['revision'] != app.revision or
                review['desired_state'] != app.desired_state):
            raise HTTPException(409, 'Review has changed')
        evidence = review.get('evidence')
        if (not evidence or evidence['id'] != str(body.evidence_id) or evidence['result'] != 'confirmed' or
                (now() - datetime.fromisoformat(evidence['checked_at'])).total_seconds() > 60):
            raise HTTPException(409, 'Refresh and confirm current cluster evidence')
        if not review.get('request'):
            env = db.get(UserEnvironment, app.environment_id)
            if not env or env.status in ('deleted', 'suspended'):
                raise HTTPException(409, 'Application environment is inactive')
            request = {'issuer': principal.issuer, 'subject': principal.subject}
            app.resources = {**app.resources, 'write_review': {**review, 'request': request}}
            db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                              action=REVIEW_REQUESTED, target_id=audit_target(app.id, review['id'])))
            db.commit()
        return review_out(app)

    return router
