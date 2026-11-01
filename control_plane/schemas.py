from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Name = Annotated[str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")]


class WorkspaceSpec(Input):
    kind: Literal["workspace"]
    name: Name
    cpu_millicores: int = Field(default=500, ge=250, le=4000)
    memory_mib: int = Field(default=1024, ge=512, le=8192)
    storage_gib: int = Field(default=5, ge=1, le=40)


class DatabaseSpec(Input):
    kind: Literal["database"]
    name: Name
    environment: Literal["development", "production"] = "development"


class BucketSpec(Input):
    kind: Literal["bucket"]
    name: Name
    environment: Literal["development", "production"] = "development"
    storage_gib: int = Field(default=1, ge=1, le=40)


class AgentSpec(Input):
    kind: Literal["agent"]
    name: Name
    workspace_id: str = Field(min_length=36, max_length=36)


class DeploymentSpec(Input):
    kind: Literal["deployment"]
    name: Name
    image: str = Field(max_length=512, pattern=r"^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$")
    port: int = Field(default=8080, ge=1024, le=65535)
    cpu_millicores: int = Field(default=250, ge=100, le=4000)
    memory_mib: int = Field(default=512, ge=128, le=8192)


ResourceRequest = Annotated[WorkspaceSpec | DatabaseSpec | BucketSpec | AgentSpec | DeploymentSpec, Field(discriminator="kind")]


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ResourceOut(Output):
    id: str
    tenant_id: str
    project_id: str
    kind: str
    name: str
    status: str
    spec: dict
    created_at: datetime


class OperationOut(Output):
    id: str
    tenant_id: str
    resource_id: str
    status: str
    plan: dict | None
    created_at: datetime


class MemberInput(Input):
    subject: str = Field(min_length=1, max_length=255)
    role: Literal["admin", "developer", "viewer"]


class MemberOut(Output):
    issuer: str
    subject: str
    role: str


class QuotaInput(Input):
    project_limit: int = Field(ge=1, le=100)
    cpu_millicores: int = Field(ge=250, le=128000)
    memory_mib: int = Field(ge=512, le=262144)
    storage_gib: int = Field(ge=1, le=10240)
    resource_limit: int = Field(ge=1, le=1000)


class AuditOut(Output):
    id: str
    tenant_id: str
    issuer: str
    subject: str
    action: str
    target_id: str
    created_at: datetime
