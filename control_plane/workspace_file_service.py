"""Single-volume internal file service; not wired to the public control plane.

The caller must authenticate the user and bind this service to the correct
workspace before forwarding a request. This service's token authorizes the
whole mounted volume and is not a user identity. TLS, network isolation,
per-user secret provisioning, rate limiting and auditing are deployment gates.
"""
import asyncio
import errno
import hmac
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .workspace_files import InvalidWorkspacePath, UploadTooLarge, WorkspaceFiles


def create_app(root: str, service_token: str, *, max_file_bytes: int = 16 * 1024 * 1024, max_inflight: int = 4) -> FastAPI:
    if not isinstance(service_token, str) or len(service_token.encode('utf-8')) < 32:
        raise ValueError('A randomly generated service token of at least 32 bytes is required')
    if max_inflight < 1 or max_inflight > 16:
        raise ValueError('Invalid concurrent transfer limit')
    files = WorkspaceFiles(root, max_file_bytes=max_file_bytes)
    # Refuse startup if the volume is absent, is a symlink, or cannot be opened.
    with files._directory([]):
        pass
    token_bytes = service_token.encode('utf-8')
    transfers = asyncio.Semaphore(max_inflight)
    app = FastAPI(title='Internal workspace files', version='0.1.0',
                  description='Service-authenticated single-volume API. The caller must verify user identity and workspace ownership before proxying. Not for direct browser access.',
                  docs_url=None, redoc_url=None, openapi_url=None)
    bearer = HTTPBearer(auto_error=False, scheme_name='WorkspaceServiceToken')

    @app.middleware('http')
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def authenticate(credentials: HTTPAuthorizationCredentials | None = Security(bearer)):
        if credentials is None or credentials.scheme.lower() != 'bearer' or not hmac.compare_digest(credentials.credentials.encode('utf-8'), token_bytes):
            raise HTTPException(status_code=401, detail='Authentication required', headers={'WWW-Authenticate': 'Bearer'})

    def sanitized_error(error: Exception):
        if isinstance(error, InvalidWorkspacePath):
            return HTTPException(400, 'Invalid workspace path')
        if isinstance(error, UploadTooLarge):
            return HTTPException(413, 'Transfer exceeds the allowed size')
        if isinstance(error, FileNotFoundError):
            return HTTPException(404, 'File or directory not found')
        if isinstance(error, FileExistsError):
            return HTTPException(409, 'File or directory already exists')
        if isinstance(error, PermissionError):
            return HTTPException(403, 'File operation not permitted')
        if isinstance(error, OSError):
            if error.errno in (errno.ENOSPC, errno.EDQUOT):
                return HTTPException(507, 'Workspace storage is full')
            if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR):
                return HTTPException(400, 'Invalid workspace path')
        return HTTPException(503, 'Workspace operation unavailable')

    @app.get('/healthz', include_in_schema=False)
    def health():
        return {'status': 'ok'}

    @app.get('/v1/directories', dependencies=[Depends(authenticate)], description='List one directory in the pre-authorized workspace. An empty path selects its root.', responses={400: {'description': 'Invalid path'}, 401: {'description': 'Service authentication required'}, 404: {'description': 'Directory not found'}, 503: {'description': 'Workspace unavailable'}})
    async def list_directory(path: str = Query(default='', max_length=4096)):
        try:
            async with transfers:
                return {'entries': await run_in_threadpool(files.list_directory, path)}
        except (OSError, ValueError) as error:
            raise sanitized_error(error) from None

    @app.post('/v1/directories', status_code=201, dependencies=[Depends(authenticate)], description='Create one directory. Parents must already exist; existing entries are never overwritten.', responses={400: {'description': 'Invalid path'}, 401: {'description': 'Service authentication required'}, 404: {'description': 'Parent directory not found'}, 409: {'description': 'Destination already exists'}, 507: {'description': 'Storage full'}})
    async def create_directory(path: str = Query(min_length=1, max_length=4096)):
        try:
            async with transfers:
                await run_in_threadpool(files.create_directory, path)
        except (OSError, ValueError) as error:
            raise sanitized_error(error) from None
        return {'created': True}

    @app.get('/v1/file', dependencies=[Depends(authenticate)], response_class=Response,
             description='Read a regular file up to the configured per-transfer limit; responses are not cacheable.',
             responses={200: {'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}, 'description': 'File content'}, 400: {'description': 'Invalid path'}, 401: {'description': 'Service authentication required'}, 404: {'description': 'File not found'}, 413: {'description': 'Transfer limit exceeded'}})
    async def read_file(path: str = Query(min_length=1, max_length=4096)):
        try:
            async with transfers:
                data = await run_in_threadpool(files.read_file, path)
                return Response(content=data, media_type='application/octet-stream')
        except (OSError, ValueError) as error:
            raise sanitized_error(error) from None

    @app.put('/v1/file', status_code=201, dependencies=[Depends(authenticate)],
             description='Upload raw bytes up to the configured per-transfer limit. Existing files are preserved unless overwrite=true; concurrent overwrites are last-writer-wins.',
             responses={400: {'description': 'Invalid path'}, 401: {'description': 'Service authentication required'}, 404: {'description': 'Parent directory not found'}, 409: {'description': 'Destination already exists'}, 413: {'description': 'Transfer limit exceeded'}, 507: {'description': 'Storage full'}},
             openapi_extra={'requestBody': {'required': True, 'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}})
    async def write_file(request: Request, path: str = Query(min_length=1, max_length=4096), overwrite: bool = False):
        # Buffering is bounded by both per-file size and the concurrency limit.
        # Until streaming is implemented, reject oversized bodies before publish.
        try:
            async with transfers:
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > max_file_bytes:
                        raise UploadTooLarge('Transfer exceeds the allowed size')
                    body.extend(chunk)
                count = await run_in_threadpool(files.write_file, path, [bytes(body)], overwrite=overwrite)
                return {'size': count}
        except (OSError, ValueError) as error:
            raise sanitized_error(error) from None

    return app


def server_options(environment: dict[str, str]) -> dict:
    """Fail closed if a non-loopback listener lacks server-side TLS."""
    host = environment.get('WORKSPACE_SERVICE_HOST', '127.0.0.1')
    if host not in ('127.0.0.1', '::1', '0.0.0.0', '::'):
        raise ValueError('Unsupported listener address')
    try:
        port = int(environment.get('WORKSPACE_SERVICE_PORT', '8090'))
    except ValueError:
        raise ValueError('Invalid listener port') from None
    if not 1 <= port <= 65535:
        raise ValueError('Invalid listener port')
    cert = environment.get('WORKSPACE_TLS_CERT', '')
    key = environment.get('WORKSPACE_TLS_KEY', '')
    if bool(cert) != bool(key):
        raise ValueError('Both TLS certificate and key are required')
    if host not in ('127.0.0.1', '::1') and not cert:
        raise ValueError('A non-loopback listener requires TLS')
    if cert and (not Path(cert).is_file() or not Path(key).is_file()):
        raise ValueError('TLS certificate or key is unavailable')
    # Access logs must remain disabled: query strings may contain user filenames.
    result = {'host': host, 'port': port, 'access_log': False}
    if cert:
        result.update(ssl_certfile=cert, ssl_keyfile=key)
    return result


def main():
    import uvicorn
    options = server_options(os.environ)
    app = create_app(os.environ['WORKSPACE_ROOT'], os.environ['WORKSPACE_SERVICE_TOKEN'])
    uvicorn.run(app, **options)


if __name__ == '__main__':
    main()
