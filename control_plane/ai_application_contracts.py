"""P1 application contracts; transport and persistence adapters must supply trusted records.

This module does not enable a public execution endpoint. Admission is evaluated before
every model/tool call; budget reservation and request deduplication belong to the adapter.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


Identifier = Annotated[str, Field(min_length=1, max_length=255)]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class ModelRevision(FrozenContract):
    provider: Identifier
    model: Identifier
    route_revision: Identifier
    temperature: float = Field(default=0, ge=0, le=2, allow_inf_nan=False)
    max_output_tokens: int = Field(gt=0)


class KnowledgeRevision(FrozenContract):
    collection_id: UUID
    file_id: Identifier
    file_digest: Digest
    index_revision: Identifier
    index_digest: Digest
    grant_id: UUID


class SkillRevision(FrozenContract):
    skill_id: Identifier
    revision: Identifier
    content: str = Field(min_length=1, max_length=64000)


class ToolRevision(FrozenContract):
    tool_id: Identifier
    definition_digest: Digest
    grant_id: UUID
    # References only. The credential resolver must check grant scope at execution.
    credential_ref: Identifier | None = None


class AgentSnapshot(FrozenContract):
    schema_version: Literal[1] = 1
    instructions: str = Field(min_length=1, max_length=64000)
    model: ModelRevision
    knowledge: tuple[KnowledgeRevision, ...] = ()
    skills: tuple[SkillRevision, ...] = ()
    tools: tuple[ToolRevision, ...] = ()

    @model_validator(mode='after')
    def unique_bindings(self):
        for values in (
            [(k.collection_id, k.file_id) for k in self.knowledge],
            [s.skill_id for s in self.skills],
            [t.tool_id for t in self.tools],
        ):
            if len(set(values)) != len(values):
                raise ValueError('duplicate_snapshot_binding')
        return self

    def digest(self) -> str:
        # Array order is intentional: instruction/skill order can change behaviour.
        encoded = json.dumps(self.model_dump(mode='json'), sort_keys=True,
                             separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        return sha256(encoded.encode('utf-8')).hexdigest()


class RunInput(FrozenContract):
    """Browser input: no owner, sponsor, prompt, tools, model override or history."""
    session_id: UUID
    request_id: UUID
    message: str = Field(min_length=1, max_length=32000)


class CreateAssistantInput(FrozenContract):
    request_id: UUID
    project_id: UUID
    template: Literal['knowledge-assistant-v1'] = 'knowledge-assistant-v1'
    # Owner/environment and LibreChat author are always resolved by the server.


class UpdateDraftInput(FrozenContract):
    expected_revision: int = Field(ge=1)
    snapshot: AgentSnapshot


class PublishInput(FrozenContract):
    request_id: UUID
    expected_draft_revision: int = Field(ge=1)
    expected_snapshot_digest: Digest
    accepted_preview_id: UUID


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str


@dataclass(frozen=True)
class ApplicationVersion:
    project_id: UUID
    version_id: UUID
    kind: Literal['draft', 'release']
    revision: int
    owner: Identity
    sponsor: Identity
    snapshot: AgentSnapshot


@dataclass(frozen=True)
class SessionBinding:
    session_id: UUID
    project_id: UUID
    version_id: UUID
    snapshot_digest: str
    actor: Identity


@dataclass(frozen=True)
class AccessFacts:
    """Fresh, server-loaded authorization scoped to one actor and version.

    The adapter loads these from current records, never from request JSON, model
    output or the release snapshot. Knowledge/tool sets must contain only grants
    valid for this application, actor and sponsor, including credential scopes.
    """
    project_id: UUID
    version_id: UUID
    actor: Identity
    sponsor: Identity
    actor_active: bool
    owner_active: bool
    sponsor_active: bool
    application_enabled: bool
    invited: bool
    sponsor_authorized: bool
    budget_available: bool
    allowed_model_routes: frozenset[tuple[str, str, str]]
    ready_knowledge: frozenset[KnowledgeRevision]
    allowed_tools: frozenset[ToolRevision]


@dataclass(frozen=True)
class Admission:
    allowed: bool
    code: str


def admit_call(version: ApplicationVersion, session: SessionBinding,
               request: RunInput, actor: Identity, facts: AccessFacts) -> Admission:
    """Pure policy check; never resolves credentials or retries a billed operation."""
    if (facts.actor != actor or facts.project_id != version.project_id or
            facts.version_id != version.version_id or facts.sponsor != version.sponsor):
        return Admission(False, 'authority_mismatch')
    if (session.session_id != request.session_id or session.actor != actor or
            session.project_id != version.project_id or session.version_id != version.version_id or
            session.snapshot_digest != version.snapshot.digest()):
        return Admission(False, 'session_version_mismatch')
    if not (facts.actor_active and facts.owner_active and facts.sponsor_active):
        return Admission(False, 'account_unavailable')
    if not facts.application_enabled:
        return Admission(False, 'application_disabled')
    if version.kind == 'draft' and actor != version.owner:
        return Admission(False, 'draft_owner_required')
    if version.kind == 'release' and actor != version.owner and not facts.invited:
        return Admission(False, 'application_access_denied')
    if version.sponsor != version.owner or not facts.sponsor_authorized:
        # P1 supports creator-sponsored calls only; team billing is a later contract.
        return Admission(False, 'sponsor_not_authorized')
    model = version.snapshot.model
    if (model.provider, model.model, model.route_revision) not in facts.allowed_model_routes:
        return Admission(False, 'model_unavailable')
    if not set(version.snapshot.knowledge) <= facts.ready_knowledge:
        return Admission(False, 'knowledge_unavailable')
    if not set(version.snapshot.tools) <= facts.allowed_tools:
        return Admission(False, 'tool_access_denied')
    if not facts.budget_available:
        return Admission(False, 'sponsor_budget_exhausted')
    return Admission(True, 'admitted')


@dataclass(frozen=True)
class PreviewAcceptance:
    preview_id: UUID
    project_id: UUID
    draft_revision: int
    snapshot_digest: str
    accepted_by: Identity
    outcome: Literal['passed', 'failed', 'running']


def admit_publication(draft: ApplicationVersion, request: PublishInput,
                      actor: Identity, preview: PreviewAcceptance) -> Admission:
    """CAS and preview checks; caller must hold the draft lock through persistence.

    General access and current grants are also checked via admit_call. Passing this
    function alone is not authorization to publish or proof of provider readiness.
    """
    if draft.kind != 'draft' or actor != draft.owner:
        return Admission(False, 'draft_owner_required')
    digest = draft.snapshot.digest()
    if (request.expected_draft_revision != draft.revision or
            request.expected_snapshot_digest != digest):
        return Admission(False, 'draft_changed')
    if (preview.preview_id != request.accepted_preview_id or
            preview.project_id != draft.project_id or
            preview.draft_revision != draft.revision or preview.snapshot_digest != digest or
            preview.accepted_by != actor or preview.outcome != 'passed'):
        return Admission(False, 'preview_confirmation_required')
    return Admission(True, 'admitted')
