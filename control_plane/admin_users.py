"""Read-only resource inventory for provisioned platform identities."""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from .capacity import COMPONENTS, GIB
from .database import get_db
from .models import (
    CapacityAccount, ModelWallet, StorageMeasurement, Tenant, UserApplication,
    UserEnvironment, WorkspaceFileService, WorkspaceVolumeBinding, now,
)
from .settings import get_settings


class StorageSampleOut(BaseModel):
    component: str
    used_bytes: int | None
    budget_bytes: int | None
    status: str
    observed_at: datetime | None


class ModelUsageOut(BaseModel):
    status: str
    observed_at: datetime | None
    allocated_usd_micros: int | None
    spent_usd_micros: int | None
    remaining_usd_micros: int | None
    total_tokens: int | None


class AdminUserOut(BaseModel):
    environment_id: UUID
    issuer: str
    subject: str
    workspace_name: str
    workspace_status: str
    file_service_status: str | None
    namespace: str | None
    created_at: datetime
    plan_id: str | None
    plan_name: str | None
    application_limit: int
    application_count: int
    files: StorageSampleOut
    model_usage: ModelUsageOut


class AdminUsersOut(BaseModel):
    items: list[AdminUserOut]
    next_offset: int | None


class AdminApplicationOut(BaseModel):
    id: UUID
    project: str
    status: str
    desired_state: str
    created_at: datetime


class AdminUserDetailOut(BaseModel):
    user: AdminUserOut
    storage: list[StorageSampleOut]
    applications: list[AdminApplicationOut]
    next_application_offset: int | None


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def sample_status(status, observed_at, max_age, checked_at):
    if not observed_at:
        return 'not_metered' if status == 'not_metered' else 'unknown'
    return 'stale' if status == 'ok' and (checked_at - utc(observed_at)).total_seconds() > max_age else status


def storage_out(component, used, budget, status, at, settings, checked_at):
    return StorageSampleOut(component=component, used_bytes=used if at else None, budget_bytes=budget,
                            status=sample_status(status, at, settings.storage_sample_max_age_seconds, checked_at),
                            observed_at=utc(at))


def inventory_query():
    count = select(func.count(UserApplication.id)).where(
        UserApplication.environment_id == UserEnvironment.id, UserApplication.status != 'deleted'
    ).correlate(UserEnvironment).scalar_subquery()
    return (select(UserEnvironment, Tenant.name.label('workspace_name'),
                   WorkspaceFileService.status.label('file_status'), WorkspaceVolumeBinding.namespace_name,
                   CapacityAccount.plan, ModelWallet.sample.label('wallet'),
                   ModelWallet.status.label('wallet_status'), ModelWallet.observed_at.label('wallet_at'),
                   StorageMeasurement.used_bytes.label('file_bytes'),
                   StorageMeasurement.status.label('file_measurement_status'),
                   StorageMeasurement.observed_at.label('file_at'), count.label('app_count'))
            .select_from(UserEnvironment)
            .join(Tenant, Tenant.id == UserEnvironment.first_tenant_id)
            .outerjoin(WorkspaceFileService, WorkspaceFileService.environment_id == UserEnvironment.id)
            .outerjoin(WorkspaceVolumeBinding, WorkspaceVolumeBinding.environment_id == UserEnvironment.id)
            .outerjoin(CapacityAccount, CapacityAccount.environment_id == UserEnvironment.id)
            .outerjoin(ModelWallet, ModelWallet.environment_id == UserEnvironment.id)
            .outerjoin(StorageMeasurement, (StorageMeasurement.environment_id == UserEnvironment.id) &
                       (StorageMeasurement.component == 'files')))


def user_out(row, settings, checked_at):
    env = row.UserEnvironment
    plan = row.plan or {}
    wallet = row.wallet if row.wallet_at and row.wallet else {}
    return AdminUserOut(
        environment_id=env.id, issuer=env.issuer, subject=env.subject, workspace_name=row.workspace_name,
        workspace_status=env.status, file_service_status=row.file_status, namespace=row.namespace_name,
        created_at=utc(env.created_at), plan_id=plan.get('id'), plan_name=plan.get('name'),
        application_limit=plan.get('application_limit', settings.default_application_limit),
        application_count=row.app_count,
        files=storage_out('files', row.file_bytes, env.storage_gib * GIB, row.file_measurement_status,
                          row.file_at, settings, checked_at),
        model_usage=ModelUsageOut(
            status=sample_status(row.wallet_status, row.wallet_at, 180, checked_at), observed_at=utc(row.wallet_at),
            allocated_usd_micros=wallet.get('allocated_usd_micros'),
            spent_usd_micros=wallet.get('spent_usd_micros'),
            remaining_usd_micros=wallet.get('remaining_usd_micros'),
            total_tokens=wallet.get('total_tokens'),
        ),
    )


def create_admin_users_router(admin_dependency):
    router = APIRouter(prefix='/v1/admin/users', tags=['administration'])

    @router.get('', response_model=AdminUsersOut)
    def list_users(q: str = Query('', max_length=255), limit: int = Query(25, ge=1, le=100),
                   offset: int = Query(0, ge=0), _principal=Depends(admin_dependency), db=Depends(get_db)):
        query = inventory_query()
        if search := q.strip():
            query = query.where(or_(
                UserEnvironment.subject.icontains(search, autoescape=True),
                UserEnvironment.id == search,
                Tenant.name.icontains(search, autoescape=True),
                WorkspaceVolumeBinding.namespace_name == search,
            ))
        rows = db.execute(query.order_by(UserEnvironment.created_at.desc(), UserEnvironment.id)
                          .limit(limit + 1).offset(offset)).all()
        settings, checked_at = get_settings(), now()
        return AdminUsersOut(items=[user_out(row, settings, checked_at) for row in rows[:limit]],
                             next_offset=offset + limit if len(rows) > limit else None)

    @router.get('/{environment_id}', response_model=AdminUserDetailOut,
                responses={404: {'description': 'Platform user workspace not found'}})
    def user_detail(environment_id: UUID, application_limit: int = Query(25, ge=1, le=100),
                    application_offset: int = Query(0, ge=0), _principal=Depends(admin_dependency), db=Depends(get_db)):
        env_id = str(environment_id)
        row = db.execute(inventory_query().where(UserEnvironment.id == env_id)).one_or_none()
        if row is None:
            raise HTTPException(404, 'Platform user workspace not found')
        settings, checked_at = get_settings(), now()
        user = user_out(row, settings, checked_at)
        measured = {sample.component: sample for sample in db.scalars(select(StorageMeasurement).where(
            StorageMeasurement.environment_id == env_id, StorageMeasurement.component.in_(COMPONENTS[1:])))}
        samples = [user.files]
        for kind in COMPONENTS[1:]:
            sample = measured.get(kind)
            samples.append(storage_out(kind, sample.used_bytes if sample else None, sample.budget_bytes if sample else None,
                                       sample.status if sample else None, sample.observed_at if sample else None,
                                       settings, checked_at))
        apps = db.execute(select(UserApplication.id, UserApplication.project_path, UserApplication.status,
                                UserApplication.desired_state, UserApplication.created_at).where(
            UserApplication.environment_id == env_id, UserApplication.status != 'deleted')
            .order_by(UserApplication.created_at.desc(), UserApplication.id)
            .limit(application_limit + 1).offset(application_offset)).all()
        return AdminUserDetailOut(
            user=user, storage=samples,
            applications=[AdminApplicationOut(id=a.id, project=a.project_path, status=a.status,
                                            desired_state=a.desired_state, created_at=utc(a.created_at))
                          for a in apps[:application_limit]],
            next_application_offset=application_offset + application_limit if len(apps) > application_limit else None,
        )

    return router
