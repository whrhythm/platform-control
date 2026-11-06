"""Exercise the existing HTTP gateway used by native LibreChat Agents.

Provider responses are synthetic. These tests establish wire compatibility and
failure handling, not model quality, live RAG readiness or sponsor billing.
"""
import json
from types import SimpleNamespace

import certifi
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from control_plane import programming
from control_plane.settings import Settings


@pytest.fixture
def gateway(monkeypatch):
    calls = []
    replies = []
    key = 'sk-' + 'a' * 48

    def provider(request):
        calls.append(json.loads(request.content))
        assert request.headers['authorization'] == 'Bearer ' + key
        result = replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    original = httpx.AsyncClient
    monkeypatch.setattr(programming.httpx, 'AsyncClient',
        lambda **kw: original(transport=httpx.MockTransport(provider)))

    async def identity(db, settings, supplied, client):
        assert supplied == key
        return SimpleNamespace(id='synthetic-owner')

    async def space(db, settings, supplied, client, *, require_runtime, environment):
        assert require_runtime is False  # A business Agent must not launch OpenHands.
        return 'unused-namespace', ''

    monkeypatch.setattr(programming, 'resolve_identity', identity)
    monkeypatch.setattr(programming, 'resolve_space', space)
    settings = Settings(litellm_models='model', capabilities_enabled=True,
        capability_chat_model='model', agent_tasks_enabled=True,
        workspace_agent_image='configured', litellm_url='http://provider',
        litellm_master_key='synthetic', workspace_service_ca=certifi.where())

    class DB:
        def get(self, model, identifier):
            return None

        def rollback(self):
            pass

    app = FastAPI()
    app.include_router(programming.router)
    app.dependency_overrides[programming.get_db] = lambda: DB()
    app.dependency_overrides[programming.get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client, {'Authorization': 'Bearer ' + key}, calls, replies


def event(delta, finish=None):
    return 'data: ' + json.dumps({'id': 'synthetic-completion', 'object': 'chat.completion.chunk',
        'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}) + '\n\n'


def test_native_agent_tool_round_trip_preserves_ids_results_and_citations(gateway):
    client, headers, calls, replies = gateway
    tools = [{'type': 'function', 'function': {'name': 'file_search',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}}]
    history = [{'role': 'user', 'content': 'How many days?'}]
    tool_call = {'id': 'call-policy', 'type': 'function',
                 'function': {'name': 'file_search', 'arguments': '{"query":"return policy"}'}}
    first = event({'role': 'assistant', 'tool_calls': [{'index': 0, **tool_call}]}, 'tool_calls') + 'data: [DONE]\n\n'
    replies.append(httpx.Response(200, text=first, headers={'content-type': 'text/event-stream'}))
    response = client.post('/agent/v1/chat/completions', headers=headers,
        json={'model': 'codechat-chat', 'stream': True, 'messages': history, 'tools': tools, 'tool_choice': 'auto'})
    assert response.status_code == 200 and response.text == first
    assert calls[0]['tools'] == tools and calls[0]['tool_choice'] == 'auto'
    history += [
        {'role': 'assistant', 'content': None, 'tool_calls': [tool_call]},
        {'role': 'tool', 'tool_call_id': 'call-policy', 'content': '7 days [policy-v1.md#L4]'},
    ]
    final = event({'role': 'assistant', 'content': '7 days [policy-v1.md#L4].'}, 'stop') + 'data: [DONE]\n\n'
    replies.append(httpx.Response(200, text=final, headers={'content-type': 'text/event-stream'}))
    response = client.post('/agent/v1/chat/completions', headers=headers,
        json={'model': 'codechat-chat', 'stream': True, 'messages': history, 'tools': tools})
    assert response.status_code == 200 and response.text == final
    assert calls[1]['messages'] == history
    assert all(call['model'] == 'model' for call in calls)
    assert len(calls) == 2


@pytest.mark.parametrize('status,expected', [(400, 400), (401, 401), (403, 403), (429, 429), (500, 503), (504, 503)])
def test_provider_failure_is_not_retried_or_exposed(gateway, status, expected):
    client, headers, calls, replies = gateway
    replies.append(httpx.Response(status, text='private provider error and credentials'))
    response = client.post('/agent/v1/chat/completions', headers=headers,
        json={'model': 'codechat-chat', 'stream': True, 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == expected
    assert 'private provider' not in response.text
    assert len(calls) == 1


def test_unknown_model_submission_result_does_not_replay(gateway):
    client, headers, calls, replies = gateway
    replies.append(httpx.ReadTimeout('provider may already have generated a response'))
    response = client.post('/agent/v1/chat/completions', headers=headers,
        json={'model': 'codechat-chat', 'stream': True, 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == 503 and len(calls) == 1
