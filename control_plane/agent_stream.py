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
        self.activity_sent = None
        self.activity_flush = 0
        self.characters = 0

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
            self.characters, self.activity_sent = 0, None
        elif response_id and self.response_id and response_id != self.response_id:
            # Provider retry supersedes the partial answer instead of appending it twice.
            self.text, self.calls = '', {}
            self.response_id = response_id
            self.characters = 0
        choice = choices[0]
        delta = choice.get('delta') or {}
        # Report activity, never the provider's private reasoning or tool arguments.
        phase = None
        if delta.get('reasoning_content') or delta.get('reasoning'):
            phase = 'thinking'
        if isinstance(delta.get('content'), str) and delta['content']:
            self.text = (self.text + delta['content'])[:32768]
            phase = 'answering'
            self.characters += len(delta['content'])
        for call in delta.get('tool_calls') or []:
            index = call.get('index', 0)
            if index not in self.calls and len(self.calls) >= 16:
                continue
            current = self.calls.setdefault(index, {'name': '', 'arguments': ''})
            function = call.get('function') or {}
            phase = 'preparing_action'
            self.characters += len(function.get('arguments') or '')
            current['name'] = (current['name'] + (function.get('name') or ''))[:128]
            # Only the finish tool's public message is ever decoded for display.
            if current['name'] == 'finish':
                phase = 'answering'
                current['arguments'] = (current['arguments'] + (function.get('arguments') or ''))[:131072]
                try:
                    value = from_json(current['arguments'], allow_partial='trailing-strings')
                    if isinstance(value, dict) and isinstance(value.get('message'), str):
                        self.text = value['message'][:32768]
                except ValueError:
                    pass
        if phase:
            value = (phase, min(self.characters, 1000000))
            if (self.activity_sent is None or self.activity_sent[0] != phase or
                    self.clock() - self.activity_flush >= 1):
                self.context.progress('model_progress', self.identifier,
                                      phase=phase, characters=value[1])
                self.activity_sent, self.activity_flush = value, self.clock()
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
