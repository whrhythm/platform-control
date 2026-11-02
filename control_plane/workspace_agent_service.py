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


def chunk(task_id: str, content: str = '', *, finish=False) -> str:
    value = {'id': 'chatcmpl-' + task_id[:24], 'object': 'chat.completion.chunk',
             'created': int(time.time()), 'model': 'OpenHands',
             'choices': [{'index': 0, 'delta': {'content': content} if content else {},
                          'finish_reason': 'stop' if finish else None}]}
    return 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'


def execute_sdk(prompt, api_key, root, base_url, model, emit, max_iterations, cancel=None):
    # Import only inside the runner image; the control plane does not embed an agent.
    from openhands.sdk import Agent, Conversation, LLM, Tool
    from openhands.sdk.conversation import get_agent_final_response
    from openhands.sdk.event import ActionEvent
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.terminal import TerminalTool

    def progress(event):
        if isinstance(event, ActionEvent) and event.tool_name != 'finish':
            labels = {'terminal': '正在运行命令或测试', 'file_editor': '正在读取或修改文件'}
            emit(labels.get(event.tool_name, '正在执行编程工具') + '…\n\n')

    llm = LLM(model='openai/' + model, base_url=base_url, api_key=api_key,
              max_output_tokens=8192, timeout=180, num_retries=1,
              reasoning_effort=None, log_completions=False, caching_prompt=False)
    agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)])
    conversation = Conversation(agent=agent, workspace=str(root), callbacks=[progress],
                                visualizer=None, max_iteration_per_run=max_iterations)
    finished = threading.Event()
    def watch_cancel():
        while not finished.wait(0.2):
            if cancel is not None and cancel.is_set():
                conversation.pause()
                return
    threading.Thread(target=watch_cancel, daemon=True).start()
    try:
        conversation.send_message(prompt)
        conversation.run()
        return get_agent_final_response(conversation.state.events) or '本轮执行已结束，请查看个人空间中的文件。'
    finally:
        finished.set()
        conversation.close()


def create_app(root, state_dir, token, base_url, model, *, runner=execute_sdk, max_iterations=40):
    root, state_dir = Path(root).resolve(strict=True), Path(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if len(token) < 32 or not base_url or not model:
        raise ValueError('Agent runner is not configured')
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

        def work():
            try:
                result = runner(body.prompt, body.api_key, root, base_url, model, events.put, max_iterations, cancel)
                text = str(result).replace(body.api_key, '[redacted]')
                record.write_text(json.dumps({'status': 'cancelled' if cancel.is_set() else 'completed', 'text': text}, ensure_ascii=False))
                events.put(text)
            except Exception:
                record.write_text(json.dumps({'status': 'failed'}))
                events.put('编程任务未完成。请检查模型额度和运行状态，再发送新消息重试。')
            finally:
                finished.set()
                events.put(None)
                lock.release()

        threading.Thread(target=work, daemon=True).start()

        async def stream():
            try:
                yield chunk(body.task_id, 'OpenHands 已连接到你的个人空间，开始处理。\n\n')
                while True:
                    try:
                        value = events.get_nowait()
                    except queue.Empty:
                        yield ': heartbeat\n\n'
                        await asyncio.sleep(1)
                        continue
                    if value is None:
                        break
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
                     os.environ['AGENT_LLM_MODEL'], max_iterations=int(os.environ.get('AGENT_MAX_ITERATIONS', '40')))
    uvicorn.run(app, **server_options(os.environ))


if __name__ == '__main__':
    main()
