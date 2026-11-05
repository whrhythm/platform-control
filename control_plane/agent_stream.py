"""Bounded public answer projection from SDK token deltas (never reasoning/tool arguments)."""
import time
from uuid import uuid4

from pydantic_core import from_json


class AnswerStream:
    def __init__(self, context, clock=time.monotonic):
        self.context, self.clock = context, clock
        self.identifier = None
        self.text = ''
        self.calls = {}
        self.sent = None
        self.last_flush = 0
        self.closed = True
        self.response_id = None

    def __call__(self, chunk):
        # SDK supplies LiteLLM models; plain dicts also make the transport contract testable.
        data = chunk.model_dump() if hasattr(chunk, 'model_dump') else chunk
        choices = data.get('choices') or []
        if not choices:
            return
        response_id = data.get('id')
        if self.closed:
            self.identifier = str(uuid4())
            self.text, self.calls, self.sent = '', {}, None
            self.closed, self.response_id = False, response_id
        elif response_id and self.response_id and response_id != self.response_id:
            # Provider retry supersedes the partial answer instead of appending it twice.
            self.text, self.calls = '', {}
            self.response_id = response_id
        choice = choices[0]
        delta = choice.get('delta') or {}
        if isinstance(delta.get('content'), str):
            self.text = (self.text + delta['content'])[:32768]
        for call in delta.get('tool_calls') or []:
            index = call.get('index', 0)
            if index not in self.calls and len(self.calls) >= 16:
                continue
            current = self.calls.setdefault(index, {'name': '', 'arguments': ''})
            function = call.get('function') or {}
            current['name'] = (current['name'] + (function.get('name') or ''))[:128]
            # Only the finish tool's public message is ever decoded for display.
            if current['name'] == 'finish':
                current['arguments'] = (current['arguments'] + (function.get('arguments') or ''))[:131072]
                try:
                    value = from_json(current['arguments'], allow_partial='trailing-strings')
                    if isinstance(value, dict) and isinstance(value.get('message'), str):
                        self.text = value['message'][:32768]
                except ValueError:
                    pass
        self.flush(force=choice.get('finish_reason') is not None)
        if choice.get('finish_reason') is not None:
            self.closed = True

    def flush(self, *, force=False):
        if not self.identifier or self.text == self.sent:
            return
        if not force and self.sent is not None and self.clock() - self.last_flush < 0.25:
            return
        if self.text or self.sent:
            self.context.progress('assistant', self.identifier, text=self.text)
            self.sent, self.last_flush = self.text, self.clock()
