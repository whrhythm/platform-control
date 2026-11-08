"""Admission fence for operations addressing a retiring code project's directory."""
from fastapi import HTTPException
from sqlalchemy import select

from .models import CodeProjectDeletion, WorkspaceProject


def require_available_project_path(db, environment_id, path):
    paths = db.scalars(select(WorkspaceProject.project_path)
        .join(CodeProjectDeletion, CodeProjectDeletion.project_id == WorkspaceProject.id)
        .where(WorkspaceProject.environment_id == environment_id, CodeProjectDeletion.completed.is_(False)))
    if any(path == root or path.startswith(root + '/') or root.startswith(path + '/') for root in paths):
        raise HTTPException(409, 'project_deletion_in_progress')
