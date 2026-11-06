from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from control_plane.ai_application_contracts import (
    AccessFacts, AgentSnapshot, ApplicationVersion, Identity, KnowledgeRevision,
    ModelRevision, PreviewAcceptance, PublishInput, RunInput, SessionBinding,
    SkillRevision, ToolRevision, admit_call, admit_publication,
)


@pytest.fixture
def scenario():
    owner, actor = Identity('issuer', 'creator'), Identity('issuer', 'visitor')
    snapshot = AgentSnapshot(instructions='Answer from authorized sources.',
        model=ModelRevision(provider='LiteLLM', model='test-model', route_revision='1', max_output_tokens=100),
        knowledge=(KnowledgeRevision(collection_id=uuid4(), file_id='policy', file_digest='a'*64,
            index_revision='1', index_digest='b'*64, grant_id=uuid4()),),
        tools=(ToolRevision(tool_id='file_search', definition_digest='c'*64, grant_id=uuid4()),),
        skills=(SkillRevision(skill_id='citations', revision='1', content='Cite the source.'),))
    version = ApplicationVersion(uuid4(), uuid4(), 'release', 1, owner, owner, snapshot)
    session = SessionBinding(uuid4(), version.project_id, version.version_id, snapshot.digest(), actor)
    request = RunInput(session_id=session.session_id, request_id=uuid4(), message='What is the policy?')
    facts = AccessFacts(version.project_id, version.version_id, actor, owner, True, True, True,
        True, True, True, True, frozenset({('LiteLLM', 'test-model', '1')}),
        frozenset(snapshot.knowledge), frozenset(snapshot.tools))
    return version, session, request, actor, facts


def test_visitor_uses_creator_budget_not_own_identity(scenario):
    version, session, request, actor, facts = scenario
    assert actor != version.sponsor
    assert admit_call(*scenario).allowed
    assert admit_call(version, session, request, actor,
        replace(facts, sponsor=actor)).code == 'authority_mismatch'
    assert admit_call(version, session, request, actor,
        replace(facts, budget_available=False)).code == 'sponsor_budget_exhausted'


@pytest.mark.parametrize('field,value,code', [
    ('invited', False, 'application_access_denied'),
    ('actor_active', False, 'account_unavailable'),
    ('owner_active', False, 'account_unavailable'),
    ('sponsor_active', False, 'account_unavailable'),
    ('application_enabled', False, 'application_disabled'),
    ('sponsor_authorized', False, 'sponsor_not_authorized'),
    ('allowed_model_routes', frozenset(), 'model_unavailable'),
    ('ready_knowledge', frozenset(), 'knowledge_unavailable'),
    ('allowed_tools', frozenset(), 'tool_access_denied'),
])
def test_revocation_after_success_applies_to_next_call(scenario, field, value, code):
    version, session, request, actor, facts = scenario
    assert admit_call(*scenario).allowed
    assert admit_call(version, session, request, actor, replace(facts, **{field: value})).code == code


@pytest.mark.parametrize('field,value', [
    ('session_id', uuid4()), ('project_id', uuid4()), ('version_id', uuid4()),
    ('actor', Identity('other-issuer', 'visitor')), ('snapshot_digest', '0'*64),
])
def test_session_cannot_be_replayed_in_other_context(scenario, field, value):
    version, session, request, actor, facts = scenario
    assert admit_call(version, replace(session, **{field: value}), request, actor, facts).code == 'session_version_mismatch'


@pytest.mark.parametrize('field', ['owner', 'sponsor', 'instructions', 'model', 'tools', 'messages', 'ephemeralAgent'])
def test_browser_cannot_override_published_configuration(scenario, field):
    request = scenario[2].model_dump()
    with pytest.raises(ValidationError):
        RunInput.model_validate({**request, field: 'injected'})


def test_guest_has_no_draft_access(scenario):
    version, session, request, actor, facts = scenario
    assert admit_call(replace(version, kind='draft'), session, request, actor, facts).code == 'draft_owner_required'


def test_owner_can_preview_without_invitation(scenario):
    version, session, request, _, facts = scenario
    assert admit_call(replace(version, kind='draft'), replace(session, actor=version.owner), request,
        version.owner, replace(facts, actor=version.owner, invited=False)).allowed


def test_snapshot_is_deeply_immutable_and_stably_serialized(scenario):
    snapshot = scenario[0].snapshot
    restored = AgentSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored == snapshot and restored.digest() == snapshot.digest()
    with pytest.raises(ValidationError):
        snapshot.model.model = 'different'
    with pytest.raises(ValidationError):
        snapshot.skills[0].content = 'changed'
    with pytest.raises(ValidationError):
        AgentSnapshot.model_validate({**snapshot.model_dump(), 'tools': snapshot.tools * 2})


@pytest.mark.parametrize('part', ['instructions', 'model', 'knowledge', 'skills', 'tools'])
def test_every_behavior_change_invalidates_confirmation(scenario, part):
    version = scenario[0]
    draft = replace(version, kind='draft')
    preview = PreviewAcceptance(uuid4(), draft.project_id, 1, draft.snapshot.digest(), draft.owner, 'passed')
    request = PublishInput(request_id=uuid4(), expected_draft_revision=1,
        expected_snapshot_digest=draft.snapshot.digest(), accepted_preview_id=preview.preview_id)
    assert admit_publication(draft, request, draft.owner, preview).allowed
    data = draft.snapshot.model_dump()
    if part == 'instructions':
        data[part] = 'New instructions'
    elif part == 'model':
        data[part]['route_revision'] = '2'
    else:
        data[part] = ()
    changed = replace(draft, snapshot=AgentSnapshot.model_validate(data))
    assert admit_publication(changed, request, draft.owner, preview).code == 'draft_changed'
    updated = request.model_copy(update={'expected_snapshot_digest': changed.snapshot.digest()})
    assert admit_publication(changed, updated, draft.owner, preview).code == 'preview_confirmation_required'


@pytest.mark.parametrize('field,value', [
    ('project_id', uuid4()), ('accepted_by', Identity('issuer', 'visitor')),
    ('outcome', 'running'), ('outcome', 'failed'), ('draft_revision', 2),
])
def test_wrong_or_incomplete_preview_cannot_publish(scenario, field, value):
    draft = replace(scenario[0], kind='draft')
    preview = PreviewAcceptance(uuid4(), draft.project_id, 1, draft.snapshot.digest(), draft.owner, 'passed')
    request = PublishInput(request_id=uuid4(), expected_draft_revision=1,
        expected_snapshot_digest=draft.snapshot.digest(), accepted_preview_id=preview.preview_id)
    assert admit_publication(draft, request, draft.owner, replace(preview, **{field:value})).code == 'preview_confirmation_required'


def test_retained_index_does_not_override_revoked_grant(scenario):
    version, session, request, actor, facts = scenario
    old = version.snapshot.knowledge[0]
    regranted = old.model_copy(update={'grant_id': uuid4()})
    assert admit_call(version, session, request, actor,
        replace(facts, ready_knowledge=frozenset({regranted}))).code == 'knowledge_unavailable'
