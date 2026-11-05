"""Run in the pinned orchestrator against a local Postman HTTP simulation."""
import os
import time
from contextlib import contextmanager
from types import SimpleNamespace

from control_plane.agent_sdk import run_agent


@contextmanager
def sessions():
    yield None


class Context:
    task_id = 'synthetic-stream-test'
    sessions = staticmethod(sessions)
    stopped = False

    def __init__(self):
        self.updates = []

    def current(self, db):
        return SimpleNamespace(config={'mcp_servers': []}), None

    def progress(self, kind, identifier, **data):
        self.updates.append((time.monotonic(), data['text']))


context = Context()
settings = SimpleNamespace(litellm_url=os.environ['AGENT_STREAM_MODEL_URL'],
    workspace_agent_max_iterations=3, agent_task_seconds=60)
result = run_agent(context, '介绍能力，不调用项目工具。', 'test', 'synthetic-key', settings, readonly=True)
ended = time.monotonic()
assert len(context.updates) >= 3
assert context.updates[0][0] < ended - 1
assert 0 < len(context.updates[0][1]) < len(result)
assert context.updates[-1][1] == result
assert 'SYNTHETIC_PRIVATE_REASONING' not in str(context.updates)
print('RESULT: actual SDK streamed %d public updates before completion; private reasoning excluded' % len(context.updates))
