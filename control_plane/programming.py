"""OpenAI-compatible gateway for LibreChat's existing chat interface."""
import hashlib
import hmac
import json
import re
import ssl

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from .database import get_db
from .auth import Principal
from .models import ModelCredential, UserEnvironment, WorkspaceFileService, WorkspaceVolumeBinding
from .settings import get_settings

router = APIRouter(prefix='/agent/v1', tags=['programming'])


def resolve_capability(model, settings):
    """Route names are server configuration, never browser-supplied destinations."""
    allowed = tuple(value.strip() for value in settings.litellm_models.split(',') if value.strip())
    if settings.capabilities_enabled:
        routes = (settings.capability_chat_route, settings.capability_coding_route)
        if (not all(routes) or len(set(routes)) != 2 or
                any(route == 'OpenHands' or route in allowed for route in routes)):
            raise HTTPException(503, 'Capability routing is misconfigured')
        if model == settings.capability_coding_route:
            return 'coding', None
        if model == settings.capability_chat_route:
            actual = settings.capability_chat_model
            if not actual or actual not in allowed:
                raise HTTPException(503, 'Chat capability is not configured')
            return 'chat', actual
    if model == 'OpenHands':
        return 'coding', None
    if isinstance(model, str) and model in allowed:
        return 'chat', model
    raise HTTPException(400, 'Unsupported capability or model')


async def resolve_identity(db, settings, key, client):
    if not key.startswith('sk-') or not 35 <= len(key) <= 512:
        raise HTTPException(401, 'Invalid model credential')
    try:
        result = await client.get(settings.litellm_url.rstrip('/') + '/key/info',
            params={'key': hashlib.sha256(key.encode()).hexdigest()},
            headers={'Authorization': 'Bearer ' + settings.litellm_master_key})
        info = result.json().get('info') if result.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, 'Model gateway is unavailable') from None
    if not isinstance(info, dict) or info.get('blocked') is True:
        raise HTTPException(401, 'Model credential is not active')
    user = info.get('user_id', '')
    if not isinstance(user, str) or not re.fullmatch(r'codechat-[0-9a-f]{64}', user):
        raise HTTPException(401, 'Model identity is not linked')
    env = db.scalar(select(UserEnvironment).where(UserEnvironment.identity_hash == user[9:]))
    credential = None if env is None else db.scalar(select(ModelCredential).where(
        ModelCredential.issuer == env.issuer, ModelCredential.subject == env.subject, ModelCredential.status == 'ready'))
    try:
        cipher = Fernet(settings.model_credential_encryption_key.encode())
        actual = cipher.decrypt(credential.encrypted_key.encode()).decode() if credential else ''
    except (InvalidToken, ValueError):
        raise HTTPException(503, 'Model identity is unavailable') from None
    if not hmac.compare_digest(actual, key) or env is None or env.status in ('deleted', 'suspended'):
        raise HTTPException(401, 'Model identity is not linked')
    return env


async def resolve_space(db, settings, key, client, *, require_runtime=True):
    env = await resolve_identity(db, settings, key, client)
    cipher = Fernet(settings.model_credential_encryption_key.encode())
    service = db.get(WorkspaceFileService, env.id)
    binding = db.get(WorkspaceVolumeBinding, env.id)
    ns = 'codechat-user-' + env.identity_hash[:40]
    if not require_runtime:
        return ns, ''
    if not service or service.status != 'ready' or not binding or binding.namespace_name != ns:
        raise HTTPException(409, 'Personal space is being prepared')
    try:
        token = cipher.decrypt(service.encrypted_token.encode()).decode()
    except InvalidToken:
        raise HTTPException(503, 'Personal space is unavailable') from None
    return ns, token


async def model_principal(authorization: str = Header(default=''), db=Depends(get_db), settings=Depends(get_settings)):
    if not authorization.startswith('Bearer ') or not settings.litellm_master_key:
        raise HTTPException(401, 'Model credential required')
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        env = await resolve_identity(db, settings, authorization[7:], client)
    return Principal(issuer=env.issuer, subject=env.subject)


def agent_prompt(messages, *, python_base='', pip_index=''):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
        raise HTTPException(400, 'Text conversation required')
    history = []
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('system', 'user', 'assistant'):
            raise HTTPException(400, 'Text conversation required')
        content = message.get('content')
        if isinstance(content, list):
            if not all(isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str) for part in content):
                raise HTTPException(400, 'Use personal space to upload project files')
            content = '\n'.join(part['text'] for part in content)
        if not isinstance(content, str):
            raise HTTPException(400, 'Text conversation required')
        history.append(f"{message['role']}: {content}")
    if messages[-1].get('role') != 'user':
        raise HTTPException(400, 'A user instruction is required')
    platform = ('需要发布时，使用终端执行 python -m control_plane.publish_cli publish <工程相对目录>。'
                '该工具提交后台构建、推送和部署任务，提交后立即结束本轮对话并报告任务ID和当前状态。'
                '严禁循环查询、反复重新发布或创建后台重试脚本；失败后报告失败并等待用户新指令。'
                '不得为了绕过平台构建错误删除、简化或替换用户Dockerfile及业务代码。'
                '提交成功不等于发布成功，只有平台状态running且有真实地址时才能称已发布。'
                '可用 python -m control_plane.publish_cli list 查询应用，或 start/stop/delete <应用ID> 管理生命周期。'
                '不要尝试连接宿主 Docker daemon，不要读取或展示环境中的平台/模型凭据。')
    if python_base:
        platform += '新建 Python Dockerfile 时优先使用集群内部基础镜像：' + python_base + '。'
    if pip_index:
        platform += ('平台已配置 Python HTTPS 软件源，个人虚拟环境的 pip 会自动使用。'
                     'Python Dockerfile 在安装依赖的 RUN 之前声明 ARG PIP_INDEX_URL，平台构建任务会提供同一软件源。'
                     '不要覆盖 PIP_CACHE_DIR；下载和安装失败应明确报告原因，不要无限轮询。')
    platform += ('需要关系型数据库时执行 python -m control_plane.database_cli ensure <工程目录>，申请共享 PostgreSQL 中的独立数据库。'
                 '用 python -m control_plane.database_cli list 查看就绪状态，不要循环轮询。'
                 '就绪后用 python -m control_plane.database_cli run <工程目录> -- <程序及参数> 运行迁移或数据库测试。'
                 '该工具为子进程注入 DATABASE_URL，业务代码从环境读取，严禁将连接密码打印或写入源码、文件、聊天。'
                 '发布同一工程时平台自动交付连接配置；编译阶段不能访问数据库，迁移应在运行阶段执行。')
    platform += ('发布后的应用固定以 UID/GID 10001:10001 运行，HTTP 服务监听 0.0.0.0:8080。'
                 '源文件可能是 0600；Dockerfile 的 COPY 使用 --chown=10001:10001，最终 USER 10001:10001，'
                 '不要依赖镜像内另一个用户的 UID，也不要通过放宽用户磁盘文件权限绕过。')
    platform += ('持久对象文件可使用用户私有 MinIO 桶。先 python -m control_plane.object_cli status 查询一次就绪状态；'
                 '用 python -m control_plane.object_cli run <工程目录> -- <程序及参数> 为测试进程自动注入 S3 连接。'
                 '业务代码读取 S3_ENDPOINT_URL、S3_BUCKET 和 CODECHAT_OBJECT_PREFIX，AWS 凭据由平台提供，'
                 '不要写入源码、文件或聊天。发布同一工程时自动交付。停止/删除应用不会删除对象。')
    return ('你是 CodeChat 的 OpenHands 编程助手。用户的持久个人文件位于当前工作目录。'
            '根据最后一条用户指令实际读取、创建、修改文件并运行必要的测试；不要只展示代码。'
            '先查看现有文件，避免覆盖无关项目。在用户要求的新工程子目录工作。'
            '依赖缓存和构建产物放在个人工作目录中。不要声称未执行的测试、镜像构建或部署已成功。'
            'Python 项目依赖使用工程内 .venv，下载缓存使用已配置的个人缓存目录；不要修改执行器自身的系统环境。'
            '用中文简要回答实际修改的相对路径、测试结果及下一步。以下是聊天上下文，最后一条为本次任务：\n\n'
            + platform + '\n\n' + '\n\n'.join(history))


@router.post('/chat/completions')
async def completions(request: Request, authorization: str = Header(default=''),
                      x_codechat_conversation: str = Header(default=''), db=Depends(get_db), settings=Depends(get_settings)):
    if not settings.workspace_agent_image or not settings.litellm_url or not settings.litellm_master_key:
        raise HTTPException(503, 'Programming is not configured')
    if not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Model credential required')
    raw = bytearray()
    async for data in request.stream():
        raw.extend(data)
        if len(raw) > 512 * 1024:
            raise HTTPException(413, 'Conversation exceeds the supported size')
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, 'Invalid conversation') from None
    if not isinstance(body, dict):
        raise HTTPException(400, 'Invalid conversation')
    model = body.get('model')
    capability, actual_model = resolve_capability(model, settings)
    context = ssl.create_default_context(cafile=settings.workspace_service_ca)
    client = httpx.AsyncClient(verify=context, timeout=httpx.Timeout(600, connect=10), trust_env=False)
    try:
        namespace, service_token = await resolve_space(db, settings, authorization[7:], client,
                                                       require_runtime=capability == 'coding')
        # Release the database transaction before waiting for a long-running agent.
        db.rollback()
        if capability == 'coding':
            if body.get('stream') is not True:
                raise HTTPException(400, 'Programming requires streaming chat')
            prompt = agent_prompt(body.get('messages'), python_base=settings.workspace_python_base_image,
                                  pip_index=settings.workspace_pip_index_url)
            task_id = hashlib.sha256((x_codechat_conversation + '\n' + prompt).encode()).hexdigest()
            target = f'https://workspace-files.{namespace}.svc:8091/v1/run'
            payload = {'task_id': task_id, 'prompt': prompt, 'api_key': authorization[7:]}
            headers = {'Authorization': 'Bearer ' + service_token}
        else:
            target = settings.litellm_url.rstrip('/') + '/v1/chat/completions'
            payload, headers = {**body, 'model': actual_model}, {'Authorization': authorization}
        upstream = await client.send(client.build_request('POST', target, json=payload, headers=headers), stream=True)
        if upstream.status_code != 200:
            status = upstream.status_code if upstream.status_code in (400, 401, 403, 409, 429) else 503
            await upstream.aclose()
            raise HTTPException(status, 'Chat execution is unavailable; check model balance or active tasks')
    except HTTPException:
        await client.aclose()
        raise
    except (httpx.HTTPError, OSError):
        await client.aclose()
        raise HTTPException(503, 'Programming environment is unavailable') from None

    async def stream():
        try:
            async for data in upstream.aiter_bytes():
                yield data
        finally:
            await upstream.aclose()
            await client.aclose()
    return StreamingResponse(stream(), media_type=upstream.headers.get('content-type', 'text/event-stream'),
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
