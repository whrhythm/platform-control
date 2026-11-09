"""Review a fixed personal Action for a single assistant project."""
from typing import Literal
from uuid import UUID, uuid5, NAMESPACE_URL

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .database import get_db
from .models import AIToolReview, AuditEvent


class ReviewDescriptor(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=48)
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=500)
    target: str = Field(min_length=1, max_length=500)
    parameters: list[str] = Field(max_length=20)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    state: Literal['approved', 'rejected', 'revoked']
    acknowledged: Literal[True]


def review_id(project_id, reference):
    return str(uuid5(NAMESPACE_URL, f'{project_id}/tool-review/{reference}'))


def review_out(row):
    return {'id': row.id, 'project_id': row.project_id, 'reference': row.reference,
            'revision': row.revision, 'state': row.state, **row.descriptor}


def require_review(db, project_id, reference, identifier, lock=False):
    row = db.get(AIToolReview, identifier, with_for_update=lock, populate_existing=lock) if identifier else None
    if not row or row.project_id != project_id or row.reference != reference or row.state != 'approved':
        raise HTTPException(409, 'ai_releases_tool_review_required')
    return row


def admin_router(admin_dependency):
    router = APIRouter(prefix='/v1/admin/assistant-tool-reviews', tags=['administration'])

    @router.get('')
    def list_reviews(principal=Depends(admin_dependency), db=Depends(get_db)):
        rows = db.scalars(select(AIToolReview).order_by(
            (AIToolReview.state == 'pending').desc(), AIToolReview.created_at.desc()).limit(100))
        return {'reviews': [review_out(row) for row in rows]}

    @router.post('/{identifier}')
    def decide(identifier: UUID, body: Decision, principal=Depends(admin_dependency), db=Depends(get_db)):
        row = db.get(AIToolReview, str(identifier), with_for_update=True)
        if not row:
            raise HTTPException(404, 'assistant_tool_review_not_found')
        if row.revision != body.revision:
            raise HTTPException(409, 'assistant_tool_review_changed')
        if (row.state, body.state) not in {('pending', 'approved'), ('pending', 'rejected'), ('approved', 'revoked')}:
            raise HTTPException(409, 'assistant_tool_review_changed')
        row.state = body.state
        row.revision += 1
        db.add(AuditEvent(tenant_id='platform', issuer=principal.issuer, subject=principal.subject,
            action='ai_release.tool_'+body.state, target_id=row.id))
        db.commit()
        return review_out(row)

    return router
