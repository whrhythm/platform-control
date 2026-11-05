"""Single trusted orchestrator; restart interrupts uncertain tasks instead of replaying them."""
import logging
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select

from .agent_execution import TaskTools, TaskStopped, cleanup
from .agent_tasks import ACTIVE, append_event, unseal
from .application_controller import Cluster
from .database import SessionLocal
from .models import AgentTask, ModelCredential, UserEnvironment
from .settings import get_settings


def finish(sessions, task_id, settings, status, code=None, text=''):
    with sessions.begin() as db:
        task = db.scalar(select(AgentTask).where(AgentTask.id == task_id).with_for_update())
        # Another slot may have completed cancellation while this runner unwound.
        # A late result must not turn an observed final state back into failure.
        if task.status not in ACTIVE:
            return
        if task.status == 'cancelling':
            status, code, text = 'cancelled', None, '任务已停止，源文件保留。'
        task.status, task.error_code, task.pending = status, code, None
        append_event(db, task, settings, 'finished', status=status, error_code=code, text=text)


def recover(sessions, settings):
    with sessions.begin() as db:
        tasks = db.scalars(select(AgentTask).where(AgentTask.status.in_(('running', 'awaiting_approval', 'cancelling'))).with_for_update())
        for task in tasks:
            task.status = 'interrupted'
            task.error_code = 'agent_interrupted_verify_files'
            task.pending = None
            append_event(db, task, settings, 'finished', status='interrupted',
                         text='执行器已重启。任务未自动重放，请检查文件和应用状态后再继续。')


def iteration(sessions, cluster, settings, run_agent):
    with sessions() as db:
        dirty = [(task.id, task.environment_id) for task in db.scalars(select(AgentTask).where(AgentTask.status.not_in(ACTIVE)))
                 if task.resources.get('job')]
    blocked = []
    for identifier, environment_id in dirty:
        try:
            cleanup(sessions, identifier, cluster)
        except Exception:
            blocked.append(environment_id)
            logging.warning('Agent cleanup pending task=%s', identifier)
    with sessions.begin() as db:
        task = db.scalar(select(AgentTask).where(AgentTask.status.in_(('queued', 'cancelling')), AgentTask.environment_id.not_in(blocked))
                         .order_by(AgentTask.created_at).with_for_update(skip_locked=True).limit(1))
        if task is None:
            return
        identifier = task.id
        if task.status == 'cancelling':
            task.status = 'cancelled'
            append_event(db, task, settings, 'finished', status='cancelled', text='任务已取消。')
            return
        env = db.get(UserEnvironment, task.environment_id)
        credential = db.scalar(select(ModelCredential).where(ModelCredential.issuer == env.issuer,
            ModelCredential.subject == env.subject, ModelCredential.status == 'ready'))
        if credential is None or env.status in ('suspended', 'deleted'):
            task.status, task.error_code = 'failed', 'agent_account_unavailable'
            return
        key = Fernet(settings.model_credential_encryption_key.encode()).decrypt(credential.encrypted_key.encode()).decode()
        prompt, config = unseal(task.encrypted_prompt, settings), task.config
        from .ai_policy import effective_policy
        if config['model'] not in effective_policy(db, settings)['allowed_models']:
            task.status, task.error_code = 'failed', 'agent_model_not_available'
            return
        prompt = '当前工作方式：' + config['strategy'] + '。当前工程：' + config['project_path'] + '。\n' + prompt
        task.status = 'running'
        append_event(db, task, settings, 'started', text='已启动编程任务。命令在独立 Kata 环境中执行。')
    try:
        tls = ssl.create_default_context(cafile=settings.workspace_service_ca)
        with httpx.Client(base_url=settings.agent_platform_url, verify=tls, timeout=45,
                          headers={'Authorization': 'Bearer ' + key}, trust_env=False, follow_redirects=False) as api:
            context = TaskTools(sessions, identifier, cluster, settings, api)
            result = run_agent(context, prompt, config['model'], key, settings, readonly=config['strategy'] != 'implement')
            finish(sessions, identifier, settings, 'completed', text=result[:32768])
    except Exception as error:
        code = str(error) if isinstance(error, TaskStopped) else 'agent_execution_failed'
        finish(sessions, identifier, settings, 'failed', code, '本轮未完成，请查看执行记录；源文件保留，未自动重放操作。')
        logging.warning('Agent task=%s status=%s', identifier, code)
    finally:
        try:
            cleanup(sessions, identifier, cluster)
        except Exception:
            logging.warning('Agent cleanup pending task=%s', identifier)


def main():
    from .agent_sdk import run_agent
    from .database import engine
    settings = get_settings()
    if not settings.agent_tasks_enabled:
        raise RuntimeError('agent_workbench_disabled')
    logging.basicConfig(level=logging.WARNING, force=True)
    engine.echo = False
    engine.hide_parameters = True
    for name in ('sqlalchemy', 'sqlalchemy.engine', 'sqlalchemy.engine.Engine', 'httpx', 'httpcore', 'openhands'):
        logging.getLogger(name).setLevel(logging.WARNING)
    recover(SessionLocal, settings)
    service_account = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    tls = ssl.create_default_context(cafile=str(service_account / 'ca.crt'))
    def slot():
        while True:
            try:
                with httpx.Client(base_url='https://kubernetes.default.svc/', verify=tls, timeout=15,
                                  headers={'Authorization': 'Bearer ' + (service_account / 'token').read_text().strip()}, trust_env=False) as client:
                    iteration(SessionLocal, Cluster(client), settings, run_agent)
            except Exception as error:
                logging.warning('Agent reconciliation unavailable type=%s', type(error).__name__)
            time.sleep(2)
    if not 1 <= settings.agent_concurrency <= 4:
        raise RuntimeError('agent_concurrency_invalid')
    with ThreadPoolExecutor(max_workers=settings.agent_concurrency) as pool:
        for _ in range(settings.agent_concurrency):
            pool.submit(slot)


if __name__ == '__main__':
    main()
