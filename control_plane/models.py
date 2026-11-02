from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uuid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


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
    first_project_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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

