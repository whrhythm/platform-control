from contextlib import asynccontextmanager
import asyncio
from datetime import datetime
import hashlib
import json
import hmac
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import Principal, current_principal
from .database import engine, get_db
from .models import AuditEvent, Membership, Operation, Project, Resource, Tenant, TestOrder, UserEnvironment, WorkspaceProvisioningIntent
from .application_review_api import create_review_router
from .admin_users import create_admin_users_router
from .schemas import AuditOut, MemberInput, MemberOut, OperationOut, QuotaInput, ResourceOut, ResourceRequest
from .settings import get_settings
from .litellm import LiteLLMError
from .model_credentials import provision_model_key
from .user_environments import identity_digest, plan_user_environment
from .workspace_gateway import call_files, own_environment
from .models import WorkspaceFileService
from .programming import router as programming_router
from .programming import model_principal
from .project_databases import create_router as create_database_router
from .object_storage import create_router as create_object_router
from .applications import create_router as create_application_router
from .capacity import create_router as create_capacity_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Schema changes are an explicit deployment step, never an API side effect.
    with engine.connect() as connection:
        if connection.scalar(text("SELECT version_num FROM alembic_version")) != "0028_assistant_sharing":
            raise RuntimeError("Database schema is not current; run alembic upgrade head")
    yield


app = FastAPI(title="Tenant Control Plane", version="0.1.0", lifespan=lifespan)
from .code_execution import router as code_execution_router
app.include_router(code_execution_router)
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def safe_validation(request: Request, error: RequestValidationError):
    if request.url.path.startswith('/internal/v1/workspace/knowledge-libraries'):
        return JSONResponse(status_code=422, content={'detail': 'knowledge_invalid_request'})
    if request.url.path.startswith('/internal/v1/workspace/ai-projects'):
        return JSONResponse(status_code=422, content={'detail': 'ai_projects_invalid_request'})
    if request.url.path.endswith('/acceptance'):
        return JSONResponse(status_code=422, content={'detail': 'invalid_acceptance_input'})
    if request.url.path.startswith('/v1/admin/accounts') or request.url.path.endswith('/credential'):
        return JSONResponse(status_code=422, content={'detail':'invalid_account_or_credential_input'})
    return await request_validation_exception_handler(request, error)
app.include_router(programming_router)
app.include_router(create_application_router(model_principal, prefix='/agent/v1'))
app.include_router(create_database_router(model_principal, prefix='/agent/v1', agent=True))
app.include_router(create_object_router(model_principal, prefix='/agent/v1', agent=True))
workspace_transfer_slots = asyncio.Semaphore(4)
app.add_middleware(CORSMiddleware, allow_origins=[origin for origin in (get_settings().public_origin, get_settings().additional_public_origin) if origin], allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])


@app.middleware('http')
async def workspace_no_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/internal/v1/workspace'):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    project_limit: int
    cpu_millicores: int
    memory_mib: int
    storage_gib: int
    resource_limit: int
    created_at: datetime


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    tenant_id: str
    name: str
    status: str
    created_at: datetime


class TestOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    tenant_id: str
    status: str
    created_at: datetime


class UserEnvironmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    identity_hash: str
    namespace: str
    storage_gib: int
    status: str
    first_tenant_id: str
    first_project_id: str | None
    created_at: datetime
    mode: str
    executable: bool
    runtime_requirement: dict
    plan: list[dict]
    blockers: list[str]


def environment_response(environment: UserEnvironment) -> UserEnvironmentOut:
    plan = plan_user_environment(environment.issuer, environment.subject, storage_gib=environment.storage_gib)
    return UserEnvironmentOut(
        id=environment.id,
        identity_hash=environment.identity_hash,
        namespace=plan["namespace"],
        storage_gib=environment.storage_gib,
        status=environment.status,
        first_tenant_id=environment.first_tenant_id,
        first_project_id=environment.first_project_id,
        created_at=environment.created_at,
        mode=plan["mode"],
        executable=plan["executable"],
        runtime_requirement=plan["runtime_requirement"],
        plan=plan["objects"],
        blockers=plan["blockers"],
    )


def membership(db: Session, principal: Principal, tenant_id: str) -> Membership:
    member = db.scalar(select(Membership).where(Membership.tenant_id == tenant_id, Membership.issuer == principal.issuer, Membership.subject == principal.subject))
    if not member:
        # Do not disclose whether a tenant exists.
        raise HTTPException(status_code=404, detail="Tenant not found")
    return member


def authorize(db: Session, principal: Principal, tenant_id: str, roles: set[str]):
    member = membership(db, principal, tenant_id)
    if member.role not in roles:
        raise HTTPException(status_code=403, detail="Insufficient tenant role")
    return member


def audit(db: Session, principal: Principal, tenant_id: str, action: str, target_id: str):
    db.add(AuditEvent(tenant_id=tenant_id, issuer=principal.issuer, subject=principal.subject, action=action, target_id=target_id))


def usage(db: Session, tenant_id: str) -> dict:
    active = Resource.status != "cancelled"
    sums = db.execute(select(func.coalesce(func.sum(Resource.cpu_millicores), 0), func.coalesce(func.sum(Resource.memory_mib), 0), func.coalesce(func.sum(Resource.storage_gib), 0), func.count(Resource.id)).where(Resource.tenant_id == tenant_id, active)).one()
    return dict(zip(("cpu_millicores", "memory_mib", "storage_gib", "resource_limit"), sums))


@app.get("/v1/config", tags=["identity"])
def config():
    settings = get_settings()
    return {"issuer": settings.oidc_issuer, "client_id": settings.oidc_client_id, "scope": f"openid profile email {settings.oidc_required_scope}", "provisioning_mode": "plan"}


@app.get("/healthz", tags=["health"])
def health():
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
def ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ready"}


@app.get("/v1/me", tags=["identity"])
def me(principal: Principal = Depends(current_principal)):
    return {"issuer": principal.issuer, "subject": principal.subject, "platform_admin": principal.is_platform_admin}


@app.get("/v1/me/environment", response_model=UserEnvironmentOut, tags=["environments"], description="Return the authenticated user's environment record, if requested. This is a plan, not evidence that a volume or sandbox exists.")
def get_my_environment(response: Response, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    environment = db.scalar(select(UserEnvironment).where(UserEnvironment.issuer == principal.issuer, UserEnvironment.subject == principal.subject))
    if environment is None:
        raise HTTPException(status_code=404, detail="Environment not requested")
    return environment_response(environment)


@app.post("/v1/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED, tags=["tenants"])
def create_tenant(body: TenantCreate, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    settings = get_settings()
    tenant = Tenant(name=body.name, owner_issuer=principal.issuer, owner_subject=principal.subject, project_limit=settings.default_project_limit, cpu_millicores=settings.default_cpu_millicores, memory_mib=settings.default_memory_mib, storage_gib=settings.default_storage_gib, resource_limit=settings.default_resource_limit)
    db.add(tenant)
    try:
        db.flush()
        db.add(Membership(tenant_id=tenant.id, issuer=principal.issuer, subject=principal.subject, role="owner"))
        audit(db, principal, tenant.id, "tenant.create", tenant.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Personal tenant already exists")
    return tenant


@app.post("/v1/me/personal-tenant", response_model=TenantOut, tags=["tenants"], description="Get or create the authenticated user's personal tenant. Repeated calls do not create duplicate tenants.")
def ensure_personal_tenant(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    def find_owned():
        return db.scalar(select(Tenant).where(Tenant.owner_issuer == principal.issuer, Tenant.owner_subject == principal.subject))

    tenant = find_owned()
    if tenant is not None:
        return tenant
    settings = get_settings()
    tenant = Tenant(name="个人空间", owner_issuer=principal.issuer, owner_subject=principal.subject, project_limit=settings.default_project_limit, cpu_millicores=settings.default_cpu_millicores, memory_mib=settings.default_memory_mib, storage_gib=settings.default_storage_gib, resource_limit=settings.default_resource_limit)
    db.add(tenant)
    try:
        db.flush()
        db.add(Membership(tenant_id=tenant.id, issuer=principal.issuer, subject=principal.subject, role="owner"))
        audit(db, principal, tenant.id, "tenant.create", tenant.id)
        db.commit()
    except IntegrityError:
        # The unique owner constraint arbitrates simultaneous first logins.
        db.rollback()
        tenant = find_owned()
        if tenant is None:
            raise
    return tenant


@app.post('/internal/v1/litellm/key', tags=['internal'], description='Service-authenticated, user-scoped credential provisioning. Never expose the response to browsers.')
def provision_litellm_key(response: Response, service_token: str = Header(default='', alias='X-CodeChat-Service-Token'), principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    settings = get_settings()
    if len(settings.librechat_service_token) < 32 or not hmac.compare_digest(service_token, settings.librechat_service_token):
        raise HTTPException(status_code=403, detail='Service authentication failed')
    response.headers['Cache-Control'] = 'no-store'
    tenant = ensure_personal_tenant(principal, db)
    ensure_personal_environment(principal, tenant.id, db)
    try:
        key = provision_model_key(db, principal, settings, tenant.id)
    except LiteLLMError:
        raise HTTPException(status_code=503, detail='Model credential unavailable') from None
    return {'key': key, 'issuer': principal.issuer, 'subject': principal.subject}


def ensure_personal_environment(principal: Principal, tenant_id: str, db: Session) -> UserEnvironment:
    """Journal one identity-bound plan at login; never call a provider here."""
    identity = (UserEnvironment.issuer == principal.issuer, UserEnvironment.subject == principal.subject)
    try:
        environment = db.scalar(select(UserEnvironment).where(*identity).with_for_update())
        if environment is None:
            from .ai_policy import trial_plan
            from .models import CapacityAccount
            allowance = trial_plan(db, get_settings())
            environment = UserEnvironment(
                issuer=principal.issuer, subject=principal.subject,
                identity_hash=identity_digest(principal.issuer, principal.subject),
                storage_gib=allowance['storage_gib'],
                first_tenant_id=tenant_id, first_project_id=None,
            )
            db.add(environment)
            db.flush()
            db.add(CapacityAccount(environment_id=environment.id, plan=allowance))
            audit(db, principal, tenant_id, 'environment.create', environment.id)
        if environment.status == 'requested' and db.get(WorkspaceProvisioningIntent, environment.id) is None:
            db.add(WorkspaceProvisioningIntent(environment_id=environment.id, state='requested'))
        db.commit()
    except IntegrityError:
        db.rollback()
        # Another first login may have inserted the same identity. Its journal
        # is committed atomically with the environment, or can be backfilled.
        environment = db.scalar(select(UserEnvironment).where(*identity).with_for_update())
        if environment is None:
            raise
        if environment.status == 'requested' and db.get(WorkspaceProvisioningIntent, environment.id) is None:
            db.add(WorkspaceProvisioningIntent(environment_id=environment.id, state='requested'))
            db.commit()
    return environment


def workspace_service_principal(
    service_token: str = Header(default='', alias='X-CodeChat-Service-Token'),
    subject: str = Header(default='', alias='X-CodeChat-Subject'),
) -> Principal:
    settings = get_settings()
    if (not settings.workspace_files_enabled or not settings.oidc_issuer or len(settings.librechat_service_token) < 32 or
            not hmac.compare_digest(service_token, settings.librechat_service_token) or
            not subject or len(subject) > 255 or any(ord(c) < 32 for c in subject)):
        raise HTTPException(403, 'Workspace access unavailable')
    return Principal(issuer=settings.oidc_issuer, subject=subject)


app.include_router(create_application_router(workspace_service_principal))
from .payments import create_router as create_payments_router
app.include_router(create_payments_router(workspace_service_principal))
app.include_router(create_capacity_router(workspace_service_principal))
from .project_versions import create_router as create_versions_router
app.include_router(create_versions_router(workspace_service_principal))
from .workspace_projects import create_router as create_workspace_projects_router
app.include_router(create_workspace_projects_router(workspace_service_principal))
from .ai_projects import create_router as create_ai_projects_router
app.include_router(create_ai_projects_router(workspace_service_principal))
from .knowledge import create_router as create_knowledge_router
app.include_router(create_knowledge_router(workspace_service_principal))
from .knowledge_libraries import create_router as create_libraries_router
app.include_router(create_libraries_router(workspace_service_principal))
from .ai_trials import create_router as create_ai_trials_router
app.include_router(create_ai_trials_router(workspace_service_principal))
from .ai_releases import create_router as create_ai_releases_router
from .ai_sponsor import router as ai_sponsor_router
app.include_router(create_ai_releases_router(workspace_service_principal))
app.include_router(ai_sponsor_router)
from .business_templates import create_router as create_business_templates_router
app.include_router(create_business_templates_router(workspace_service_principal))
app.include_router(create_database_router(workspace_service_principal))
app.include_router(create_object_router(workspace_service_principal))
from .agent_tasks import create_agent_router
app.include_router(create_agent_router(workspace_service_principal))


@app.post('/internal/v1/workspace/ensure', tags=['workspace-files'])
def ensure_workspace(principal: Principal = Depends(workspace_service_principal), db: Session = Depends(get_db)):
    tenant = ensure_personal_tenant(principal, db)
    environment = ensure_personal_environment(principal, tenant.id, db)
    service = db.get(WorkspaceFileService, environment.id)
    return {'status': service.status if service else 'preparing',
            'storage_gib': environment.storage_gib}


@app.get('/internal/v1/workspace/directories', tags=['workspace-files'])
def workspace_directory(path: str = Query(default='', max_length=4096),
                        principal: Principal = Depends(workspace_service_principal), db: Session = Depends(get_db)):
    return call_files(db, principal, get_settings(), 'GET', '/v1/directories', path).json()


@app.get('/internal/v1/workspace/usage', tags=['workspace-files'])
def workspace_usage(principal: Principal = Depends(workspace_service_principal), db: Session = Depends(get_db)):
    from .workspace_gateway import own_environment
    env = own_environment(db, principal)
    result = call_files(db, principal, get_settings(), 'GET', '/v1/usage', '').json()
    result['quota_bytes'] = env.storage_gib * 1024 ** 3
    result['available_bytes'] = max(0, result['quota_bytes'] - result['used_bytes'])
    # This is the current personal volume, not a fabricated cross-provider total.
    return result


@app.post('/internal/v1/workspace/directories', tags=['workspace-files'], status_code=201)
def workspace_mkdir(path: str = Query(min_length=1, max_length=4096),
                    principal: Principal = Depends(workspace_service_principal), db: Session = Depends(get_db)):
    return call_files(db, principal, get_settings(), 'POST', '/v1/directories', path).json()


@app.get('/internal/v1/workspace/file', tags=['workspace-files'])
async def workspace_read(path: str = Query(min_length=1, max_length=4096),
                   principal: Principal = Depends(workspace_service_principal), db: Session = Depends(get_db)):
    from fastapi.concurrency import run_in_threadpool
    async with workspace_transfer_slots:
        result = await run_in_threadpool(call_files, db, principal, get_settings(), 'GET', '/v1/file', path)
    return Response(result.content, media_type='application/octet-stream', headers={
        'Cache-Control': 'no-store', **({'ETag': result.headers['etag']} if 'etag' in result.headers else {})})


@app.get('/internal/v1/workspace/archive', tags=['workspace-files'])
async def workspace_archive(path: str = Query(min_length=1, max_length=4096),
                            principal: Principal = Depends(workspace_service_principal), db: Session = Depends(get_db)):
    from fastapi.concurrency import run_in_threadpool
    async with workspace_transfer_slots:
        result = await run_in_threadpool(call_files, db, principal, get_settings(), 'GET', '/v1/archive', path)
    return Response(result.content, media_type='application/zip', headers={
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'X-CodeChat-Archive-Skipped': result.headers.get('X-CodeChat-Archive-Skipped', '0')})


@app.put('/internal/v1/workspace/file', tags=['workspace-files'], status_code=201)
async def workspace_write(request: Request, path: str = Query(min_length=1, max_length=4096), overwrite: bool = False,
                          if_match: str = Header(default='', max_length=66, pattern=r'^(?:"[0-9a-f]{64}")?$'),
                          principal: Principal = Depends(workspace_service_principal), db: Session = Depends(get_db)):
    from fastapi.concurrency import run_in_threadpool
    if overwrite and not if_match:
        raise HTTPException(428, 'Reload the file before saving')
    async with workspace_transfer_slots:
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > 16 * 1024 * 1024:
                raise HTTPException(413, 'File exceeds the transfer limit')
            content.extend(chunk)
        result = await run_in_threadpool(call_files, db, principal, get_settings(), 'PUT', '/v1/file', path,
                                         body=bytes(content), overwrite=overwrite, if_match=if_match)
    return result.json()


@app.delete('/internal/v1/workspace/file', tags=['workspace-files'])
def workspace_delete(path: str = Query(min_length=1, max_length=4096),
                     if_match: str = Header(min_length=66, max_length=66, pattern=r'^"[0-9a-f]{64}"$'),
                     principal: Principal = Depends(workspace_service_principal), db: Session = Depends(get_db)):
    return call_files(db, principal, get_settings(), 'DELETE', '/v1/file', path, if_match=if_match).json()


@app.get("/v1/tenants", response_model=list[TenantOut], tags=["tenants"])
def list_tenants(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return list(db.scalars(select(Tenant).join(Membership, Membership.tenant_id == Tenant.id).where(Membership.issuer == principal.issuer, Membership.subject == principal.subject).order_by(Tenant.created_at)))


@app.post("/v1/tenants/{tenant_id}/projects", response_model=ProjectOut, status_code=201, tags=["projects"])
def create_project(tenant_id: str, body: ProjectCreate, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    authorize(db, principal, tenant_id, {"owner", "admin", "developer"})
    # On PostgreSQL this row lock serializes the quota check across requests.
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if get_settings().require_project_entitlement:
        # Simulated payments never grant access after the explicit test mode is disabled.
        paid = get_settings().test_payments_enabled and db.scalar(select(TestOrder.id).where(TestOrder.tenant_id == tenant_id, TestOrder.status == "paid").limit(1))
        if not paid:
            raise HTTPException(status_code=403, detail="Project entitlement required")
    count = db.scalar(select(func.count(Project.id)).where(Project.tenant_id == tenant_id))
    if count >= tenant.project_limit:
        raise HTTPException(status_code=409, detail="Project quota exceeded")
    project = Project(tenant_id=tenant_id, name=body.name)
    db.add(project)
    try:
        db.flush()
        audit(db, principal, tenant_id, "project.create", project.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project name already exists in tenant")
    return project


@app.get("/v1/tenants/{tenant_id}/projects", response_model=list[ProjectOut], tags=["projects"])
def list_projects(tenant_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    membership(db, principal, tenant_id)
    return list(db.scalars(select(Project).where(Project.tenant_id == tenant_id).order_by(Project.created_at)))


@app.get("/v1/tenants/{tenant_id}/projects/{project_id}", response_model=ProjectOut, tags=["projects"])
def get_project(tenant_id: str, project_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    membership(db, principal, tenant_id)
    project = db.scalar(select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.post("/v1/tenants/{tenant_id}/projects/{project_id}/environment", response_model=UserEnvironmentOut, tags=["environments"], description="Get or create the requesting user's dedicated execution environment. Identity comes only from the verified token; one environment per user, repeated requests return the same record. Plan only: no sandbox, volume or credential is created.")
def request_environment(tenant_id: str, project_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    authorize(db, principal, tenant_id, {"owner", "admin", "developer"})
    get_project(tenant_id, project_id, principal, db)
    existing = db.scalar(select(UserEnvironment).where(UserEnvironment.issuer == principal.issuer, UserEnvironment.subject == principal.subject))
    if existing is not None:
        return environment_response(existing)
    settings = get_settings()
    from .ai_policy import trial_plan
    from .models import CapacityAccount
    allowance = trial_plan(db, settings)
    environment = UserEnvironment(issuer=principal.issuer, subject=principal.subject, identity_hash=identity_digest(principal.issuer, principal.subject), storage_gib=allowance['storage_gib'], first_tenant_id=tenant_id, first_project_id=project_id)
    db.add(environment)
    try:
        db.flush()
        db.add(CapacityAccount(environment_id=environment.id, plan=allowance))
        audit(db, principal, tenant_id, "environment.create", environment.id)
        db.commit()
    except IntegrityError:
        # The unique identity constraint arbitrates simultaneous first requests.
        db.rollback()
        existing = db.scalar(select(UserEnvironment).where(UserEnvironment.issuer == principal.issuer, UserEnvironment.subject == principal.subject))
        if existing is None:
            raise
        return environment_response(existing)
    return environment_response(environment)


@app.get("/v1/tenants/{tenant_id}/projects/{project_id}/environment", response_model=UserEnvironmentOut, tags=["environments"], description="View the requesting user's execution environment plan once requested through any project.")
def get_environment(tenant_id: str, project_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    get_project(tenant_id, project_id, principal, db)
    environment = db.scalar(select(UserEnvironment).where(UserEnvironment.issuer == principal.issuer, UserEnvironment.subject == principal.subject))
    if environment is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    return environment_response(environment)


@app.get("/v1/tenants/{tenant_id}/members", response_model=list[MemberOut], tags=["members"])
def members(tenant_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    authorize(db, principal, tenant_id, {"owner", "admin"})
    return list(db.scalars(select(Membership).where(Membership.tenant_id == tenant_id)))


@app.put("/v1/tenants/{tenant_id}/members", response_model=MemberOut, tags=["members"])
def set_member(tenant_id: str, body: MemberInput, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    authorize(db, principal, tenant_id, {"owner"})
    # Membership identities are always from the server-configured issuer.
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    member = db.scalar(select(Membership).where(Membership.tenant_id == tenant_id, Membership.issuer == principal.issuer, Membership.subject == body.subject))
    if member is not None and member.role == "owner":
        raise HTTPException(status_code=409, detail="Owner cannot be reassigned through membership API")
    if member is None:
        member = Membership(tenant_id=tenant_id, issuer=principal.issuer, subject=body.subject, role=body.role)
        db.add(member)
    else:
        member.role = body.role
    audit(db, principal, tenant_id, "member.set", body.subject)
    db.commit()
    return member


@app.delete("/v1/tenants/{tenant_id}/members/{member_subject}", status_code=204, tags=["members"])
def remove_member(tenant_id: str, member_subject: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    authorize(db, principal, tenant_id, {"owner"})
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    member = db.scalar(select(Membership).where(Membership.tenant_id == tenant_id, Membership.issuer == principal.issuer, Membership.subject == member_subject))
    if member is None:
        raise HTTPException(404, "Member not found")
    if member.role == "owner":
        raise HTTPException(409, "Owner cannot be removed")
    db.delete(member)
    audit(db, principal, tenant_id, "member.remove", member_subject)
    db.commit()


@app.get("/v1/tenants/{tenant_id}/quota", tags=["quotas"])
def quota(tenant_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    membership(db, principal, tenant_id)
    tenant = db.get(Tenant, tenant_id)
    return {"limits": {key: getattr(tenant, key) for key in ("project_limit", "cpu_millicores", "memory_mib", "storage_gib", "resource_limit")}, "reserved": {**usage(db, tenant_id), "project_limit": db.scalar(select(func.count(Project.id)).where(Project.tenant_id == tenant_id))}, "measurement": "reserved_capacity_not_billing"}


@app.get("/v1/tenants/{tenant_id}/projects/{project_id}/resources", response_model=list[ResourceOut], tags=["resources"])
def resources(tenant_id: str, project_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    get_project(tenant_id, project_id, principal, db)
    return list(db.scalars(select(Resource).where(Resource.tenant_id == tenant_id, Resource.project_id == project_id).order_by(Resource.created_at)))


@app.post("/v1/tenants/{tenant_id}/projects/{project_id}/resources", response_model=ResourceOut, status_code=201, tags=["resources"], description="Reserve capacity and queue a provisioning plan. This does not create running infrastructure. Reusing the same Idempotency-Key and body returns the same resource.")
def request_resource(tenant_id: str, project_id: str, body: ResourceRequest, idempotency_key: str = Header(min_length=8, max_length=128), principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    authorize(db, principal, tenant_id, {"owner", "admin", "developer"})
    get_project(tenant_id, project_id, principal, db)
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    spec = body.model_dump()
    digest = hashlib.sha256(json.dumps({"project_id": project_id, "spec": spec}, sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(Resource).where(Resource.tenant_id == tenant_id, Resource.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.request_hash != digest:
            raise HTTPException(409, "Idempotency-Key reused with a different request")
        return existing
    if body.kind == "deployment" and not body.image.startswith(get_settings().approved_registry):
        raise HTTPException(422, "Image must use the approved registry and immutable digest")
    if body.kind == "agent":
        workspace = db.scalar(select(Resource).where(Resource.id == body.workspace_id, Resource.tenant_id == tenant_id, Resource.project_id == project_id, Resource.kind == "workspace", Resource.status != "cancelled"))
        if workspace is None:
            raise HTTPException(404, "Workspace not found")
        agents = db.scalars(select(Resource).where(Resource.tenant_id == tenant_id, Resource.project_id == project_id, Resource.kind == "agent", Resource.status != "cancelled"))
        if any(agent.spec["workspace_id"] == body.workspace_id for agent in agents):
            raise HTTPException(409, "Workspace already has an agent allocation")
    if db.scalar(select(Resource).where(Resource.tenant_id == tenant_id, Resource.project_id == project_id, Resource.kind == body.kind, Resource.name == body.name, Resource.status != "cancelled")):
        raise HTTPException(409, "Resource name already exists")
    costs = {key: spec.get(key, 0) for key in ("cpu_millicores", "memory_mib", "storage_gib")}
    reserved = usage(db, tenant_id)
    for key, cost in {**costs, "resource_limit": 1}.items():
        if reserved[key] + cost > getattr(tenant, key):
            raise HTTPException(409, f"Tenant quota exceeded: {key}")
    resource = Resource(tenant_id=tenant_id, project_id=project_id, kind=body.kind, name=body.name, spec=spec, idempotency_key=idempotency_key, request_hash=digest, **costs)
    db.add(resource)
    try:
        db.flush()
        db.add(Operation(tenant_id=tenant_id, resource_id=resource.id))
        audit(db, principal, tenant_id, "resource.request", resource.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Conflicting request; retry with the same key")
    return resource


@app.delete("/v1/tenants/{tenant_id}/resources/{resource_id}", response_model=ResourceOut, tags=["resources"], description="Cancel an unapplied request and release its reservation. Running resources require the future deprovisioning workflow.")
def cancel_resource(tenant_id: str, resource_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    authorize(db, principal, tenant_id, {"owner", "admin", "developer"})
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    resource = db.scalar(select(Resource).where(Resource.tenant_id == tenant_id, Resource.id == resource_id).with_for_update())
    if resource is None:
        raise HTTPException(404, "Resource not found")
    if resource.status == "cancelled":
        return resource
    if resource.status not in {"requested", "planned"}:
        raise HTTPException(409, "Resource must be deprovisioned before releasing quota")
    if resource.kind == "workspace":
        agents = db.scalars(select(Resource).where(Resource.tenant_id == tenant_id, Resource.project_id == resource.project_id, Resource.kind == "agent", Resource.status != "cancelled"))
        if any(agent.spec["workspace_id"] == resource.id for agent in agents):
            raise HTTPException(409, "Cancel the workspace agent first")
    resource.status = "cancelled"
    operation = db.scalar(select(Operation).where(Operation.resource_id == resource.id))
    operation.status = "cancelled"
    operation.plan = None
    audit(db, principal, tenant_id, "resource.cancel", resource.id)
    db.commit()
    return resource


@app.post("/v1/tenants/{tenant_id}/projects/{project_id}/cancel-plans", tags=["resources"], description="Atomically cancel all unapplied project resource plans. This does not delete provisioned resources or project data.")
def cancel_project_plans(tenant_id: str, project_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    authorize(db, principal, tenant_id, {"owner", "admin"})
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    project = db.scalar(select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id))
    if project is None:
        raise HTTPException(404, "Project not found")
    resources = list(db.scalars(select(Resource).where(Resource.tenant_id == tenant_id, Resource.project_id == project_id).order_by(Resource.id).with_for_update()))
    if any(resource.status not in {"requested", "planned", "cancelled"} for resource in resources):
        raise HTTPException(409, "Project has resources requiring actual deprovisioning")
    pending = [resource for resource in resources if resource.status != "cancelled"]
    if pending:
        operations = {op.resource_id: op for op in db.scalars(select(Operation).where(Operation.tenant_id == tenant_id, Operation.resource_id.in_([resource.id for resource in pending])).order_by(Operation.id).with_for_update())}
        if len(operations) != len(pending):
            raise HTTPException(409, "Resource operation missing; no plans cancelled")
        for resource in pending:
            resource.status = "cancelled"
            operations[resource.id].status = "cancelled"
            operations[resource.id].plan = None
            audit(db, principal, tenant_id, "resource.cancel", resource.id)
        db.commit()
    return {"project_id": project_id, "cancelled_count": len(pending), "mode": "plans_only", "infrastructure_deleted": False}


@app.get("/v1/tenants/{tenant_id}/operations", response_model=list[OperationOut], tags=["operations"])
def operations(tenant_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db), limit: int = Query(default=50, ge=1, le=200)):
    membership(db, principal, tenant_id)
    return list(db.scalars(select(Operation).where(Operation.tenant_id == tenant_id).order_by(Operation.created_at.desc()).limit(limit)))


@app.get("/v1/tenants/{tenant_id}/audit", response_model=list[AuditOut], tags=["audit"])
def audit_events(tenant_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db), limit: int = Query(default=50, ge=1, le=200)):
    authorize(db, principal, tenant_id, {"owner", "admin"})
    return list(db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant_id).order_by(AuditEvent.created_at.desc()).limit(limit)))


def platform_admin(principal: Principal = Depends(current_principal)):
    if not principal.is_platform_admin:
        raise HTTPException(403, "Platform administrator required")
    return principal


def require_test_payments():
    if not get_settings().test_payments_enabled:
        raise HTTPException(404, "Not found")


@app.post("/v1/tenants/{tenant_id}/test-orders", response_model=TestOrderOut, tags=["test payments"])
def create_test_order(tenant_id: str, idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128), principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    require_test_payments()
    authorize(db, principal, tenant_id, {"owner", "admin"})
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    order = db.scalar(select(TestOrder).where(TestOrder.tenant_id == tenant_id, TestOrder.idempotency_key == idempotency_key))
    if order is None:
        order = TestOrder(tenant_id=tenant_id, idempotency_key=idempotency_key)
        db.add(order)
        db.flush()
        audit(db, principal, tenant_id, "test_order.create", order.id)
        db.commit()
    return order


@app.get("/v1/tenants/{tenant_id}/test-orders/{order_id}", response_model=TestOrderOut, tags=["test payments"])
def get_test_order(tenant_id: str, order_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    require_test_payments()
    authorize(db, principal, tenant_id, {"owner", "admin"})
    order = db.scalar(select(TestOrder).where(TestOrder.tenant_id == tenant_id, TestOrder.id == order_id))
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


@app.post("/v1/admin/tenants/{tenant_id}/test-orders/{order_id}/confirm", response_model=TestOrderOut, tags=["test payments"])
def confirm_test_order(tenant_id: str, order_id: str, principal: Principal = Depends(platform_admin), db: Session = Depends(get_db)):
    require_test_payments()
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    order = db.scalar(select(TestOrder).where(TestOrder.tenant_id == tenant_id, TestOrder.id == order_id).with_for_update())
    if order is None:
        raise HTTPException(404, "Order not found")
    if order.status == "pending":
        order.status = "paid"
        audit(db, principal, tenant_id, "test_order.confirm", order.id)
        db.commit()
    return order


@app.get("/v1/admin/tenants", response_model=list[TenantOut], tags=["administration"])
def admin_tenants(principal: Principal = Depends(platform_admin), db: Session = Depends(get_db)):
    audit(db, principal, "platform", "admin.tenants.list", "platform")
    rows = list(db.scalars(select(Tenant).order_by(Tenant.created_at)))
    db.commit()
    return rows


@app.put("/v1/admin/tenants/{tenant_id}/quota", response_model=TenantOut, tags=["administration"])
def set_quota(tenant_id: str, body: QuotaInput, principal: Principal = Depends(platform_admin), db: Session = Depends(get_db)):
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    reserved = {**usage(db, tenant_id), "project_limit": db.scalar(select(func.count(Project.id)).where(Project.tenant_id == tenant_id))}
    for key, value in body.model_dump().items():
        if value < reserved[key]:
            raise HTTPException(409, f"Quota cannot be lower than reserved capacity: {key}")
        setattr(tenant, key, value)
    audit(db, principal, tenant_id, "admin.quota.set", tenant_id)
    db.commit()
    return tenant


from .ai_policy import admin_router as create_ai_admin_router, public_models
app.include_router(create_ai_admin_router(platform_admin))
from .mcp_admin import admin_router as create_mcp_admin_router, credential_router as create_mcp_credential_router
app.include_router(create_mcp_admin_router(platform_admin))
app.include_router(create_mcp_credential_router(workspace_service_principal))
app.include_router(create_review_router(platform_admin))
app.include_router(create_admin_users_router(platform_admin))
from .accounts import router as create_account_router, allowed as account_allowed
app.include_router(create_account_router(platform_admin))


@app.get('/internal/v1/workspace/account-access', tags=['identity'])
def account_access(principal: Principal = Depends(workspace_service_principal), db: Session = Depends(get_db)):
    return {'allowed': account_allowed(db, principal.issuer, principal.subject)}


@app.get('/internal/v1/workspace/models', tags=['models'])
def workspace_models(principal: Principal = Depends(workspace_service_principal), db: Session = Depends(get_db)):
    own_environment(db, principal)
    return public_models(db, get_settings())


if get_settings().static_dir:
    app.mount("/", StaticFiles(directory=Path(get_settings().static_dir), html=True), name="web")
