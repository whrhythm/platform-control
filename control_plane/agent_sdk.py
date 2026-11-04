"""OpenHands adapter: deliberately no local terminal, browser, filesystem or MCP tools."""
from typing import Literal

from pydantic import Field
from openhands.sdk import Agent, Conversation, LLM, Tool
from openhands.sdk.conversation import get_agent_final_response
from openhands.sdk.tool import ToolDefinition, ToolExecutor, register_tool
from openhands.sdk.tool.schema import Action, Observation


class ProjectRead(Action):
    operation: Literal['list', 'read', 'apps', 'databases', 'objects']
    path: str = Field(default='', max_length=4096)


class ProjectAction(Action):
    operation: Literal['list', 'read', 'write', 'mkdir', 'command', 'apps', 'publish', 'start', 'stop', 'delete', 'databases', 'database', 'objects']
    path: str = Field(default='', max_length=4096)
    content: str = Field(default='', max_length=262144, description='File content for write; shell command for command.')
    etag: str = Field(default='', description='Use the exact ETag from read when replacing an existing file.')
    application_id: str = Field(default='', max_length=36)
    connect_database: bool = Field(default=False, description='For command only: inject this project managed database connection.')
    connect_objects: bool = Field(default=False, description='For command only: inject the owner object storage connection.')


class ProjectResult(Observation):
    pass


CONTEXTS = {}


class ProjectExecutor(ToolExecutor):
    def __init__(self, context):
        self.context = context

    def __call__(self, action, conversation=None):
        if action.operation in ('list', 'read', 'write', 'mkdir'):
            result = self.context.file(action.operation, action.path, getattr(action, 'content', ''), getattr(action, 'etag', ''))
        elif action.operation == 'command':
            result = self.context.command(action.content, database=action.connect_database, objects=action.connect_objects)
        elif action.operation in ('databases', 'database', 'objects'):
            result = self.context.services(action.operation)
        else:
            result = self.context.applications('list' if action.operation == 'apps' else action.operation,
                                               getattr(action, 'application_id', ''))
        return ProjectResult.from_text(result)

    def interrupt(self):
        self.context.stopped = True


class ProjectTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state, task_id, readonly=False, **kwargs):
        return [cls(description=(
            'Operate only inside the selected persistent project. list/read use relative paths. '
            'write replaces a complete UTF-8 file; supply its ETag to avoid overwriting concurrent edits. '
            'mkdir creates one directory. command runs in a NEW isolated Kata job with /project as cwd; '
            'shell variables and processes do not persist between calls, files do. Do not launch background servers. '
            'database requests the current project database (requires confirmation), databases checks its status; '
            'objects checks owner object storage. Wait until ready before command with connect_database/connect_objects. '
            'Never print or copy connection credentials to files or chat. '
            'Use .venv for Python and .cache for dependencies; never install in the system Python. '
            'apps lists this project applications; publish/start/stop/delete request platform lifecycle actions. '
            'An accepted publish is NOT running: report actual status, do not repeatedly publish or poll. '
            'Writes and commands may wait for user approval. Read-only strategy exposes only list/read/apps.'),
            action_type=ProjectRead if readonly else ProjectAction, observation_type=ProjectResult,
            executor=ProjectExecutor(CONTEXTS[task_id]))]


register_tool(ProjectTool.name, ProjectTool)


def run_agent(context, prompt, model, key, settings, *, readonly=False):
    import tempfile
    import threading
    import time
    CONTEXTS[context.task_id] = context
    finished = threading.Event()
    # This directory is orchestration state only. No user source is mounted here.
    with tempfile.TemporaryDirectory(prefix='agent-state-') as temporary:
        llm = LLM(model='openai/' + model, base_url=settings.litellm_url.rstrip('/') + '/v1', api_key=key,
                  max_output_tokens=8192, timeout=180, num_retries=1, reasoning_effort=None,
                  log_completions=False, caching_prompt=False)
        agent = Agent(llm=llm, tools=[Tool(name=ProjectTool.name, params={'task_id': context.task_id, 'readonly': readonly})])
        conversation = Conversation(agent=agent, workspace=temporary, visualizer=None,
                                    max_iteration_per_run=settings.workspace_agent_max_iterations)
        deadline = time.monotonic() + settings.agent_task_seconds
        def monitor():
            while not finished.wait(1):
                try:
                    if time.monotonic() >= deadline:
                        context.stop_reason = 'agent_task_timed_out'
                        context.stopped = True
                    with context.sessions() as db:
                        context.current(db)
                except Exception:
                    conversation.interrupt()
                    return
        threading.Thread(target=monitor, daemon=True).start()
        try:
            conversation.send_message(prompt)
            conversation.run()
            with context.sessions() as db:
                context.current(db)
            return get_agent_final_response(conversation.state.events) or '本轮执行已结束，请查看项目文件和运行记录。'
        finally:
            finished.set()
            conversation.close()
            CONTEXTS.pop(context.task_id, None)
