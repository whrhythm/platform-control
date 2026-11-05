"""Run inside the pinned orchestrator image; a local synthetic model, no credentials or customer data."""
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from control_plane.agent_sdk import run_agent

calls = []
selected = False


class Model(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        functions = [tool['function'] for tool in body['tools']]
        project = next(tool for tool in functions if 'project' in tool['name'].lower())
        documentation = [tool for tool in functions if 'documentation' in tool['name'].lower()]
        assert bool(documentation) == selected
        operations = project['parameters']['properties']['operation']['enum']
        assert operations == ['list', 'read', 'apps', 'databases', 'objects'], operations
        if not calls:
            if selected:
                name, arguments = documentation[0]['name'], {'operation': 'call', 'server': 'deepwiki', 'tool': 'read_wiki_structure', 'arguments_json': '{"repoName":"example/project"}'}
            else:
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
        self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
        self.end_headers()
        if body.get('stream'):
            choice = payload['choices'][0]
            choice['delta'] = choice.pop('message')
            choice['delta']['tool_calls'][0]['index'] = 0
            self.wfile.write(('data: ' + json.dumps(payload) + '\n\ndata: [DONE]\n\n').encode())
        else:
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

    def progress(self, *args, **kwargs):
        pass

    def current(self, db):
        return SimpleNamespace(config={'mcp_servers': ['deepwiki'] if selected else []}), None

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
    from control_plane import agent_mcp
    def synthetic(context, server, tool, arguments):
        assert server == 'deepwiki' and tool == 'read_wiki_structure'
        assert arguments == {'repoName': 'example/project'}
        return 'synthetic reference'
    agent_mcp.execute = synthetic
    selected = True
    calls.clear()
    result = run_agent(Context(), 'Read public repository documentation', 'test', 'synthetic-key', settings, readonly=True)
    assert result == 'synthetic SDK success' and len(calls) == 2
    print('PASS: selected documentation tool uses the MCP permission gateway; unselected tool absent')
finally:
    server.shutdown()
