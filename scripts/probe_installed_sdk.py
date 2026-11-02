"""Run with the installed SDK; synthetic loopback model, temporary files only."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading


def probe(execute_sdk):
    calls = []
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            if len(calls) == 1:
                tools = body.get('tools') or []
                tool = next(t['function']['name'] for t in tools if 'terminal' in t['function']['name'])
                message = {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'call_probe', 'type': 'function', 'function': {'name': tool,
                     'arguments': json.dumps({'command': "printf sdk-tool-executed > sdk-probe.txt"})}}]}
            else:
                message = {'role': 'assistant', 'content': 'SDK probe complete'}
            result = {'id': 'probe', 'object': 'chat.completion', 'created': 1, 'model': 'probe',
                      'choices': [{'index': 0, 'message': message,
                                   'finish_reason': 'tool_calls' if len(calls) == 1 else 'stop'}],
                      'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
            encoded = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    with ThreadingHTTPServer(('127.0.0.1', 0), Model) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory(prefix='codechat-sdk-probe-') as directory:
            result = execute_sdk('Run the requested test tool, then finish.', 'sk-' + 't' * 48,
                Path(directory), f'http://127.0.0.1:{server.server_port}/v1', 'probe', lambda _text: None, 5)
            assert (Path(directory) / 'sdk-probe.txt').read_text() == 'sdk-tool-executed'
            assert 'SDK probe complete' in result
        server.shutdown()
    print('PASS installed SDK executed a terminal tool and produced a real file using a synthetic loopback model')
