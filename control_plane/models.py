from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uuid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class CodeExecution(Base):
    __tablename__ = 'code_executions'
    __table_args__ = (UniqueConstraint('environment_id', 'request_key', name='uq_code_execution_request'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    request_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(12), default='exec')
    transfer_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    period: Mapped[str] = mapped_column(String(7))
    state: Mapped[str] = mapped_column(String(20))
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AIConfiguration(Base):
    __tablename__ = 'ai_configuration'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    catalog: Mapped[list] = mapped_column(JSON, default=list)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MCPServer(Base):
    __tablename__ = 'mcp_servers'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(16), default='draft')
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    discovered: Mapped[list] = mapped_column(JSON, default=list)
    approved: Mapped[list] = mapped_column(JSON, default=list)
    addresses: Mapped[list] = mapped_column(JSON, default=list)
    encrypted_credential: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MCPCredential(Base):
    __tablename__ = 'mcp_credentials'
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), primary_key=True)
    server_id: Mapped[str] = mapped_column(ForeignKey('mcp_servers.id'), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    server_revision: Mapped[int] = mapped_column(Integer)
    encrypted_value: Mapped[str | None] = mapped_column(Text, nullable=True)


class ModelCredit(Base):
    __tablename__ = 'model_credits'
    order_id: Mapped[str] = mapped_column(ForeignKey('capacity_orders.id'), primary_key=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    amount_micros: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default='pending')
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ModelWallet(Base):
    __tablename__ = 'model_wallets'
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), primary_key=True)
    sample: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default='unknown')
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (UniqueConstraint("owner_issuer", "owner_subject"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    name: Mapped[str] = mapped_column(String(120))
    owner_issuer: Mapped[str] = mapped_column(String(512))
    owner_subject: Mapped[str] = mapped_column(String(255))
    project_limit: Mapped[int] = mapped_column(Integer)
    cpu_millicores: Mapped[int] = mapped_column(Integer, default=4000)
    memory_mib: Mapped[int] = mapped_column(Integer, default=8192)
    storage_gib: Mapped[int] = mapped_column(Integer, default=40)
    resource_limit: Mapped[int] = mapped_column(Integer, default=20)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "issuer", "subject"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("tenant_id", "name"), UniqueConstraint("id", "tenant_id"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    # A record only: no Kubernetes resources have been provisioned.
    status: Mapped[str] = mapped_column(String(30), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TestOrder(Base):
    __tablename__ = "test_orders"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ModelCredential(Base):
    __tablename__ = "model_credentials"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))
    encrypted_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserEnvironment(Base):
    """One dedicated execution environment per verified user. Plan metadata only until an apply adapter exists."""

    __tablename__ = "user_environments"
    __table_args__ = (
        UniqueConstraint("issuer", "subject"),
        CheckConstraint("status IN ('requested', 'provisioning', 'ready', 'suspended', 'deleted')", name="ck_user_environments_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))
    identity_hash: Mapped[str] = mapped_column(String(64), unique=True)
    storage_gib: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="requested")
    first_tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    first_project_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AccountAccess(Base):
    __tablename__ = 'account_access'
    issuer: Mapped[str] = mapped_column(String(512), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)


class WorkspaceVolumeBinding(Base):
    """Provider identifiers recorded by a trusted future provisioner.

    This table alone does not enforce immutability, create a volume, or grant access.
    """
    __tablename__ = "workspace_volume_bindings"
    environment_id: Mapped[str] = mapped_column(ForeignKey("user_environments.id"), primary_key=True)
    namespace_name: Mapped[str] = mapped_column(String(63), unique=True)
    namespace_uid: Mapped[str] = mapped_column(String(128), unique=True)
    pvc_uid: Mapped[str] = mapped_column(String(128), unique=True)
    pv_name: Mapped[str] = mapped_column(String(253), unique=True)
    pv_uid: Mapped[str] = mapped_column(String(128), unique=True)
    volume_handle: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkspaceProvisioningIntent(Base):
    """Durable create-only journal; in-flight writes require manual resolution."""
    __tablename__ = "workspace_provisioning_intents"
    __table_args__ = (
        CheckConstraint("state IN ('requested', 'namespace_inflight', 'namespace_recorded', 'quota_inflight', 'quota_recorded', 'policy_inflight', 'policy_recorded', 'pvc_inflight', 'pvc_recorded')", name="ck_workspace_provisioning_state"),
        UniqueConstraint('quota_uid', name='uq_workspace_intent_quota_uid'),
        UniqueConstraint('policy_uid', name='uq_workspace_intent_policy_uid'),
    )
    environment_id: Mapped[str] = mapped_column(ForeignKey("user_environments.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), default="requested")
    namespace_uid: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    quota_uid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    policy_uid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pvc_uid: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkspaceFileService(Base):
    __tablename__ = 'workspace_file_services'
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), primary_key=True)
    encrypted_token: Mapped[str] = mapped_column(String(512))
    resources: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default='requested')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CapacityAccount(Base):
    __tablename__ = 'capacity_accounts'
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), primary_key=True)
    plan: Mapped[dict] = mapped_column(JSON)
    active_order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CapacityOrder(Base):
    __tablename__ = 'capacity_orders'
    __table_args__ = (
        UniqueConstraint('environment_id', 'idempotency_key'),
        CheckConstraint("status IN ('pending_payment', 'applying', 'ready', 'failed', 'cancelled')", name='ck_capacity_order_status'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    plan: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default='pending_payment')
    payment_status: Mapped[str] = mapped_column(String(16), default='unpaid')
    stage: Mapped[str] = mapped_column(String(40), default='awaiting_test_payment')
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentReceipt(Base):
    """One payment intent per capacity order, with globally unique provider transaction."""
    __tablename__ = 'payment_receipts'
    order_id: Mapped[str] = mapped_column(ForeignKey('capacity_orders.id'), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), default='creating')
    transaction_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    checkout_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StorageMeasurement(Base):
    __tablename__ = 'storage_measurements'
    __table_args__ = (UniqueConstraint('environment_id', 'component'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    component: Mapped[str] = mapped_column(String(24))
    used_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default='unknown')
    budget_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    basis: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class UserObjectStorage(Base):
    __tablename__ = 'user_object_storage'
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), primary_key=True)
    generation: Mapped[str] = mapped_column(String(36), default=uuid)
    bucket_name: Mapped[str] = mapped_column(String(63), unique=True)
    access_key: Mapped[str] = mapped_column(String(32), unique=True)
    encrypted_secret: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default='requested')
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resources: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProjectDatabase(Base):
    __tablename__ = 'project_databases'
    __table_args__ = (UniqueConstraint('environment_id', 'project_path'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    project_path: Mapped[str] = mapped_column(String(255))
    generation: Mapped[str] = mapped_column(String(36), default=uuid)
    encrypted_password: Mapped[str] = mapped_column(String(512))
    reserved_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default='requested')
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resources: Mapped[dict] = mapped_column(JSON, default=dict)
    used_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key"),
        UniqueConstraint("id", "tenant_id"),
        ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"]),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(80))
    spec: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="requested")
    cpu_millicores: Mapped[int] = mapped_column(Integer, default=0)
    memory_mib: Mapped[int] = mapped_column(Integer, default=0)
    storage_gib: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (ForeignKeyConstraint(["resource_id", "tenant_id"], ["resources.id", "resources.tenant_id"]),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    resource_id: Mapped[str] = mapped_column(String(36), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserRegistry(Base):
    __tablename__ = 'user_registries'
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), primary_key=True)
    project_name: Mapped[str] = mapped_column(String(63), unique=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    robot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    robot_name: Mapped[str] = mapped_column(String(255))
    pull_secret_uid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    encrypted_secret: Mapped[str] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(32), default='requested')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserApplication(Base):
    __tablename__ = 'user_applications'
    __table_args__ = (UniqueConstraint('environment_id', 'project_path'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    project_path: Mapped[str] = mapped_column(String(255))
    revision: Mapped[str] = mapped_column(String(36), default=uuid)
    status: Mapped[str] = mapped_column(String(32), default='queued')
    desired_state: Mapped[str] = mapped_column(String(16), default='running')
    resources: Mapped[dict] = mapped_column(JSON, default=dict)
    image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentPreference(Base):
    __tablename__ = 'agent_preferences'
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentTask(Base):
    __tablename__ = 'agent_tasks'
    __table_args__ = (UniqueConstraint('environment_id', 'request_key'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON)
    encrypted_prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    encrypted_events: Mapped[str] = mapped_column(Text)
    pending: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resources: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkspaceProject(Base):
    """A durable owner-scoped label for an existing directory; never provisions or moves files."""
    __tablename__ = 'workspace_projects'
    __table_args__ = (UniqueConstraint('environment_id', 'project_path', name='uq_workspace_project_path'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    project_path: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(80))
    encrypted_goal: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AIProjectBinding(Base):
    """Durable private Agent creation intent; a ready binding is not a release."""
    __tablename__ = 'ai_project_bindings'
    __table_args__ = (UniqueConstraint('environment_id', 'request_id', name='uq_ai_project_request'),)
    project_id: Mapped[str] = mapped_column(ForeignKey('workspace_projects.id'), primary_key=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    native_owner_id: Mapped[str] = mapped_column(String(24))
    agent_id: Mapped[str] = mapped_column(String(80), unique=True)
    knowledge_revision_id: Mapped[str | None] = mapped_column(String(36))
    knowledge_library_id: Mapped[str | None] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(24), default='preparing')
    encrypted_initial: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AIApplication(Base):
    __tablename__ = 'ai_applications'
    project_id: Mapped[str] = mapped_column(ForeignKey('ai_project_bindings.project_id'), primary_key=True)
    active_release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    budget_micros: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class AIProjectDeletion(Base):
    __tablename__ = 'ai_project_deletions'
    project_id: Mapped[str] = mapped_column(ForeignKey('ai_project_bindings.project_id'), primary_key=True)
    plan: Mapped[dict] = mapped_column(JSON)


class CodeProjectDeletion(Base):
    __tablename__ = 'code_project_deletions'
    project_id: Mapped[str] = mapped_column(ForeignKey('workspace_projects.id'), primary_key=True)
    plan: Mapped[dict] = mapped_column(JSON)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)


class AIRelease(Base):
    __tablename__ = 'ai_releases'
    __table_args__ = (UniqueConstraint('project_id', 'request_id'), UniqueConstraint('project_id', 'number'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey('ai_applications.project_id'), index=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey('ai_project_trials.id'))
    request_id: Mapped[str] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AIAppMember(Base):
    __tablename__ = 'ai_app_members'
    project_id: Mapped[str] = mapped_column(ForeignKey('ai_applications.project_id'), primary_key=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), primary_key=True)
    native_id: Mapped[str] = mapped_column(String(24))
    encrypted_label: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(24), default='active', server_default='active')
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    budget_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AIAppEntry(Base):
    __tablename__ = 'ai_app_entries'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    release_id: Mapped[str] = mapped_column(ForeignKey('ai_releases.id'), index=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'))
    native_id: Mapped[str] = mapped_column(String(24))
    ready: Mapped[bool] = mapped_column(Boolean, default=False)


class AIAppRun(Base):
    __tablename__ = 'ai_app_runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entry_id: Mapped[str] = mapped_column(ForeignKey('ai_app_entries.id'))
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    message_id: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AIAppCall(Base):
    __tablename__ = 'ai_app_calls'
    __table_args__ = (UniqueConstraint('run_id', 'request_hash'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey('ai_applications.project_id'), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('ai_app_runs.id'), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default='reserved')
    reserved_micros: Mapped[int] = mapped_column(BigInteger)
    held_micros: Mapped[int] = mapped_column(BigInteger)
    input_rate: Mapped[str] = mapped_column(String(40))
    output_rate: Mapped[str] = mapped_column(String(40))
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AIProjectDraft(Base):
    __tablename__ = 'ai_project_drafts'
    project_id: Mapped[str] = mapped_column(ForeignKey('ai_project_bindings.project_id'), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    encrypted_config: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AIProjectTrial(Base):
    __tablename__ = 'ai_project_trials'
    __table_args__ = (UniqueConstraint('project_id', 'request_id'), UniqueConstraint('project_id', 'number'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey('ai_project_bindings.project_id'), index=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    encrypted_snapshot: Mapped[str] = mapped_column(Text)
    knowledge_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(24), default='preparing')
    encrypted_acceptance: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AITrialSession(Base):
    __tablename__ = 'ai_trial_sessions'
    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey('ai_project_trials.id'), index=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AITrialRun(Base):
    __tablename__ = 'ai_trial_runs'
    message_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey('ai_trial_sessions.conversation_id'), index=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey('ai_project_trials.id'), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeLibrary(Base):
    __tablename__ = 'knowledge_libraries'
    __table_args__ = (UniqueConstraint('environment_id', 'request_id', name='uq_library_request'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(24), default='active')
    version: Mapped[int] = mapped_column(Integer, default=1)
    head_revision_id: Mapped[str | None] = mapped_column(String(36))
    active_revision_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EmbeddingProfile(Base):
    __tablename__ = 'embedding_profiles'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    url: Mapped[str] = mapped_column(String(500))
    model: Mapped[str] = mapped_column(String(200))
    request_dimensions: Mapped[bool] = mapped_column(Boolean)
    encrypted_secret: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EmbeddingPreference(Base):
    __tablename__ = 'embedding_preference'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[str | None] = mapped_column(ForeignKey('embedding_profiles.id'))
    version: Mapped[int] = mapped_column(Integer)


class ProjectKnowledge(Base):
    __tablename__ = 'project_knowledge'
    __table_args__ = (UniqueConstraint('project_id', 'request_id', name='uq_knowledge_request'),
                     UniqueConstraint('library_id', 'request_id', name='uq_library_revision_request'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey('ai_project_bindings.project_id'), index=True, nullable=True)
    library_id: Mapped[str | None] = mapped_column(ForeignKey('knowledge_libraries.id'), index=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    digest: Mapped[str] = mapped_column(String(64))
    edit_digest: Mapped[str | None] = mapped_column(String(64))
    embedding_profile_id: Mapped[str | None] = mapped_column(ForeignKey('embedding_profiles.id'))
    encrypted_source: Mapped[str] = mapped_column(Text)
    manifest: Mapped[list] = mapped_column(JSON)
    source_bytes: Mapped[int] = mapped_column(BigInteger)
    index_bytes: Mapped[int] = mapped_column(BigInteger)
    chunks: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default='queued', index=True)
    error_code: Mapped[str | None] = mapped_column(String(60))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BusinessTemplate(Base):
    __tablename__ = 'business_templates'
    project_id: Mapped[str] = mapped_column(ForeignKey('workspace_projects.id'), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(40))
    version: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(40), default='preparing')
    encrypted_access: Mapped[str] = mapped_column(Text)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    acceptance: Mapped[dict] = mapped_column(JSON, default=dict)


class ProjectVersion(Base):
    __tablename__ = 'project_versions'
    __table_args__ = (UniqueConstraint('environment_id', 'request_key', name='uq_project_version_request'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    request_key: Mapped[str] = mapped_column(String(128))
    project_path: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(80))
    digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    encrypted_content: Mapped[bytes] = mapped_column(LargeBinary)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    source_bytes: Mapped[int] = mapped_column(BigInteger)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProjectRestore(Base):
    __tablename__ = 'project_restores'
    __table_args__ = (UniqueConstraint('environment_id', 'request_key', name='uq_project_restore_request'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid)
    environment_id: Mapped[str] = mapped_column(ForeignKey('user_environments.id'), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey('project_versions.id'))
    request_key: Mapped[str] = mapped_column(String(128))
    target: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
