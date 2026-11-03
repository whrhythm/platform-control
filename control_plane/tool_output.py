"""Bounded output filtering, including credentials split across read boundaries."""


def redacted_output(stream, sensitive):
    secrets = tuple(sorted({value for value in sensitive if value}, key=len, reverse=True))
    keep = max((len(value) for value in secrets), default=1) - 1
    pending = ''
    while True:
        chunk = stream.readline(16384)
        pending += chunk
        safe = len(pending) if not chunk else max(0, len(pending) - keep)
        while safe > 0:
            matches = [(pending.find(value), value) for value in secrets]
            matches = [(offset, value) for offset, value in matches if 0 <= offset < safe]
            if not matches:
                yield pending[:safe]
                pending = pending[safe:]
                break
            offset, value = min(matches, key=lambda item: (item[0], -len(item[1])))
            yield pending[:offset] + '[redacted]'
            pending = pending[offset + len(value):]
            safe = len(pending) if not chunk else max(0, len(pending) - keep)
        if not chunk:
            break
