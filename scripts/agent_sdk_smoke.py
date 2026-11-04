"""Run inside the pinned orchestrator image; a local synthetic model, no credentials or customer data."""
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from control_plane.agent_sdk import run_agent

calls = []


class Model(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        functions = [tool['function'] for tool in body['tools']]
        project = next(tool for tool in functions if 'project' in tool['name'].lower())
        operations = project['parameters']['properties']['operation']['enum']
        assert operations == ['list', 'read', 'apps', 'databases', 'objects'], operations
        if not calls:
            name, arguments = project['name'], {'operation': 'read', 'path': 'hello.py'}
        else:
            finish = next(tool for tool in functions if 'finish' in tool['name'].lower())
            name, arguments = finish['name'], {'message': 'synthetic SDK success'}
        calls.append(name)
        payload = {'id': 'synthetic', 'object': 'chat.completion', 'created': 1, 'model': 'test',
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'call-' + str(len(calls)), 'type': 'function',
                               'function': {'name': name, 'arguments': json.dumps(arguments)}}]}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *_):
        pass


@contextmanager
def sessions():
    yield None


class Context:
    task_id = 'smoke'
    sessions = staticmethod(sessions)
    stopped = False

    def current(self, db):
        return None

    def file(self, operation, path, content, etag):
        assert operation == 'read' and path == 'hello.py'
        return '{"content":"print(42)","etag":"test"}'


server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    settings = SimpleNamespace(litellm_url=f'http://127.0.0.1:{server.server_port}', workspace_agent_max_iterations=3, agent_task_seconds=60)
    result = run_agent(Context(), 'Read hello.py', 'test', 'synthetic-key', settings, readonly=True)
    assert result == 'synthetic SDK success', result
    assert len(calls) == 2, calls
    print('PASS: pinned SDK called only project read and finish; no local execution tools registered')
finally:
    server.shutdown()
