"""Small OpenHands SDK runner sharing the existing personal workspace volume."""
import asyncio
import hmac
import json
import os
from pathlib import Path
import queue
import threading
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field


class RunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    prompt: str = Field(min_length=1, max_length=400000)
    api_key: str = Field(min_length=32, max_length=512, repr=False)
    model: str | None = Field(default=None, min_length=1, max_length=200)


def chunk(task_id: str, content: str = '', *, finish=False, progress=False) -> str:
    value = {'id': 'chatcmpl-' + task_id[:24], 'object': 'chat.completion.chunk',
             'created': int(time.time()), 'model': 'OpenHands',
             'choices': [{'index': 0, 'delta': {('reasoning_content' if progress else 'content'): content} if content else {},
                          'finish_reason': 'stop' if finish else None}]}
    return 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'


def delivery_reply(text):
    """Extract the publication receipt without giving the model a retry turn."""
    marker = 'CODECHAT_DELIVERY_RESULT '
    for line in str(text).splitlines():
        if not line.startswith(marker) or len(line) > 2048:
            continue
        try:
            value = json.loads(line[len(marker):])
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        status = value.get('status')
        if status in ('request_failed', 'request_unknown'):
            return '发布请求未确认成功，已结束本轮，未自动重试。请检查应用状态后再操作。'
        try:
            from uuid import UUID
            identifier = str(UUID(value['id']))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if status == 'failed':
            return f'应用 {identifier} 发布失败，已停止本轮；源文件保留，未自动重复发布。请查看应用错误详情。'
        if status in ('queued', 'preparing', 'building', 'deploying', 'changing'):
            return f'应用 {identifier} 的任务已提交，当前状态：{status}。后台继续执行，本轮聊天已结束。提交不代表发布成功，请在应用列表查看结果。'
        if status in ('running', 'stopped', 'deleted'):
            return f'应用 {identifier} 当前状态：{status}。请在应用列表查看访问地址及运行详情。'
    return None


def execute_sdk(prompt, api_key, root, base_url, model, emit, max_iterations, cancel=None):
    # Import only inside the runner image; the control plane does not embed an agent.
    from openhands.sdk import Agent, Conversation, LLM, Tool
    from openhands.sdk.conversation import get_agent_final_response
    from openhands.sdk.event import ActionEvent, ObservationEvent
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.terminal import TerminalTool

    submitted = []
    reported = set()
    def progress(event):
        if isinstance(event, ActionEvent) and event.tool_name != 'finish':
            labels = {'terminal': '正在运行命令或测试', 'file_editor': '正在读取或修改文件'}
            label = labels.get(event.tool_name, '正在执行编程工具')
            if label not in reported:
                reported.add(label)
                emit({'kind': 'progress', 'text': label + '…\n\n'})
        if isinstance(event, ObservationEvent) and event.tool_name == 'terminal':
            reply = delivery_reply(getattr(event.observation, 'text', ''))
            if reply:
                submitted.append(reply)
                conversation.interrupt()

    llm = LLM(model='openai/' + model, base_url=base_url, api_key=api_key,
              max_output_tokens=8192, timeout=180, num_retries=1,
              reasoning_effort=None, log_completions=False, caching_prompt=False)
    agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)])
    # One serialized user per runner. Only the user's own virtual key reaches tools.
    os.environ['CODECHAT_MODEL_KEY'] = api_key
    conversation = Conversation(agent=agent, workspace=str(root), callbacks=[progress],
                                visualizer=None, max_iteration_per_run=max_iterations)
    finished = threading.Event()
    def watch_cancel():
        while not finished.wait(0.2):
            if cancel is not None and cancel.is_set():
                conversation.interrupt()
                return
    threading.Thread(target=watch_cancel, daemon=True).start()
    try:
        conversation.send_message(prompt)
        asyncio.run(conversation.arun())
        if submitted:
            return submitted[-1]
        return get_agent_final_response(conversation.state.events) or '本轮执行已结束，请查看个人空间中的文件。'
    finally:
        finished.set()
        conversation.close()
        os.environ.pop('CODECHAT_MODEL_KEY', None)


def create_app(root, state_dir, token, base_url, model, *, runner=execute_sdk, max_iterations=40, max_seconds=180):
    root, state_dir = Path(root).resolve(strict=True), Path(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if len(token) < 32 or not base_url or not model or not 0 < max_seconds <= 1800:
        raise ValueError('Agent runner is not configured')
    for record in state_dir.glob('*.json'):
        try:
            value = json.loads(record.read_text())
            if value.get('status') == 'running':
                record.write_text(json.dumps({'status': 'interrupted'}))
        except (OSError, ValueError):
            continue
    lock = threading.Lock()
    app = FastAPI(title='Personal OpenHands runner', docs_url=None, redoc_url=None)

    def authenticate(authorization: str = Header(default='')):
        if not hmac.compare_digest(authorization.encode(), ('Bearer ' + token).encode()):
            raise HTTPException(401, 'Authentication required')

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.post('/v1/run', dependencies=[Depends(authenticate)])
    def run(body: RunRequest):
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'A programming task is already running')
        record = state_dir / (body.task_id + '.json')
        if record.exists():
            try:
                previous = json.loads(record.read_text())
            finally:
                lock.release()
            if previous.get('status') != 'completed':
                raise HTTPException(409, 'Previous task was interrupted; send a new message')
            return StreamingResponse(iter([chunk(body.task_id, previous['text']),
                                           chunk(body.task_id, finish=True), 'data: [DONE]\n\n']),
                                     media_type='text/event-stream', headers={'Cache-Control': 'no-store'})
        # Exclusive file creation prevents an unknown prior execution being replayed.
        try:
            with record.open('x') as output:
                json.dump({'status': 'running'}, output)
        except Exception:
            lock.release()
            raise
        events = queue.Queue()
        cancel = threading.Event()
        finished = threading.Event()
        timed_out = threading.Event()

        def work():
            try:
                os.environ['CODECHAT_TASK_ID'] = body.task_id
                result = runner(body.prompt, body.api_key, root, base_url, body.model or model, events.put, max_iterations, cancel)
                text = str(result).replace(body.api_key, '[redacted]')
                if timed_out.is_set():
                    text = '本轮编程已超时并请求停止；已完成的文件保留。已提交的发布任务在后台独立执行，请单独查看其状态。'
                record.write_text(json.dumps({'status': 'timed_out' if timed_out.is_set() else 'cancelled' if cancel.is_set() else 'completed', 'text': text}, ensure_ascii=False))
                events.put(text)
            except (Exception, asyncio.CancelledError) as error:
                status = 'timed_out' if timed_out.is_set() else 'cancelled' if cancel.is_set() else 'failed'
                record.write_text(json.dumps({'status': status, 'error_type': type(error).__name__}))
                events.put('本轮编程未完成，已停止继续执行；已生成文件保留。请查看任务状态后再重试。')
            finally:
                os.environ.pop('CODECHAT_TASK_ID', None)
                finished.set()
                events.put(None)
                lock.release()

        threading.Thread(target=work, daemon=True).start()

        async def stream():
            deadline = time.monotonic() + max_seconds
            try:
                yield chunk(body.task_id, '已连接个人编程空间，开始处理。\n\n', progress=True)
                while True:
                    if time.monotonic() >= deadline and not finished.is_set():
                        timed_out.set()
                        cancel.set()
                        yield chunk(body.task_id, '本轮编程超过时间限制，已请求停止；不会自动重试。后台发布任务与聊天分别记录状态。\n\n')
                        break
                    try:
                        value = events.get_nowait()
                    except queue.Empty:
                        yield ': heartbeat\n\n'
                        await asyncio.sleep(1)
                        continue
                    if value is None:
                        break
                    if isinstance(value, dict) and value.get('kind') == 'progress':
                        yield chunk(body.task_id, value['text'].replace(body.api_key, '[redacted]'), progress=True)
                    else:
                        yield chunk(body.task_id, value.replace(body.api_key, '[redacted]'))
                yield chunk(body.task_id, finish=True)
                yield 'data: [DONE]\n\n'
            finally:
                if not finished.is_set():
                    cancel.set()
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    return app


def main():
    import logging
    import uvicorn
    from .workspace_file_service import server_options
    # SDK events belong to the personal state/chat, never shared pod logs.
    logging.disable(logging.CRITICAL)
    app = create_app(os.environ['WORKSPACE_ROOT'], os.environ['AGENT_STATE_DIR'],
                     os.environ['WORKSPACE_SERVICE_TOKEN'], os.environ['AGENT_LLM_URL'],
                     os.environ['AGENT_LLM_MODEL'], max_iterations=int(os.environ.get('AGENT_MAX_ITERATIONS', '40')),
                     max_seconds=int(os.environ.get('AGENT_MAX_SECONDS', '180')))
    uvicorn.run(app, **server_options(os.environ))


if __name__ == '__main__':
    main()
