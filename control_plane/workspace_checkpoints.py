"""Bounded source recovery points and atomic restore to a new root directory."""
import base64
import ctypes
import errno
import hashlib
import hmac
import json
import os
import stat

from .workspace_files import WorkspaceFiles, FileVersionConflict, InvalidWorkspacePath, UploadTooLarge

EXCLUDED = {'.git', '.cache', 'node_modules', '.venv', '__pycache__', '.pytest_cache', 'dist', 'build'}
MAX_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024 - 1024
RECEIPT = '.codechat-upload-restore-receipt'


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def capture(files, path, *, max_bytes=MAX_BYTES):
    base = files._parts(path)
    def scan():
        pending, entries, stamps, total, excluded = [('', base)], {}, {}, 0, []
        while pending:
            prefix, parts = pending.pop()
            if len(parts) > 40:
                raise InvalidWorkspacePath('Nested path too deep')
            with files._directory(parts) as directory:
                stamps[prefix] = files.version(os.fstat(directory))
            for item in files.list_directory('/'.join(parts)):
                relative = prefix + item['name']
                if item['name'] in EXCLUDED:
                    excluded.append(relative)
                    continue
                if len(entries) >= MAX_ENTRIES:
                    raise UploadTooLarge('Too many source entries')
                if item['kind'] == 'directory':
                    entries[relative] = {'kind': 'directory'}
                    pending.append((relative+'/', parts+[item['name']]))
                else:
                    if total + item['size'] > max_bytes:
                        raise UploadTooLarge('Source recovery point exceeds size limit')
                    content, etag = files.read_versioned('/'.join(parts+[item['name']]))
                    total += len(content)
                    if total > max_bytes:
                        raise UploadTooLarge('Source recovery point exceeds size limit')
                    with files._directory(parts) as directory:
                        info = os.stat(item['name'], dir_fd=directory, follow_symlinks=False)
                    if etag != files.version(info):
                        raise FileVersionConflict('Source changed')
                    stamps[relative] = etag
                    entries[relative] = {'kind':'file', 'data':base64.b64encode(content).decode(),
                        'sha256':hashlib.sha256(content).hexdigest(), 'size':len(content), 'mode':stat.S_IMODE(info.st_mode)&0o755}
        return {'entries':entries, 'bytes':total, 'excluded':sorted(excluded)}, stamps
    result, stamps = scan()
    check, current = scan()
    if result != check or stamps != current:
        raise FileVersionConflict('Source changed while creating recovery point')
    if len(encode(result)) > MAX_PAYLOAD_BYTES:
        raise UploadTooLarge('Recovery point metadata exceeds transfer limit')
    return result


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != {'entries', 'bytes', 'excluded'}:
        raise InvalidWorkspacePath('Invalid recovery point')
    if len(encode(payload)) > MAX_PAYLOAD_BYTES:
        raise UploadTooLarge('Recovery point exceeds transfer limit')
    entries = payload['entries']
    if type(payload['bytes']) is not int or not isinstance(payload['excluded'],list) or not all(isinstance(p,str) for p in payload['excluded']):
        raise InvalidWorkspacePath('Invalid recovery point metadata')
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise UploadTooLarge('Too many source entries')
    total = 0
    for path, item in entries.items():
        if not isinstance(path,str) or not isinstance(item,dict):
            raise InvalidWorkspacePath('Invalid entry')
        parts = WorkspaceFiles._parts(path)
        if len(parts) > 40:
            raise InvalidWorkspacePath('Nested path too deep')
        for size in range(1,len(parts)):
            if entries.get('/'.join(parts[:size]),{}).get('kind') != 'directory':
                raise InvalidWorkspacePath('Missing parent')
        if item.get('kind') == 'directory' and set(item) == {'kind'}:
            continue
        if item.get('kind') != 'file' or set(item) != {'kind','data','sha256','size','mode'}:
            raise InvalidWorkspacePath('Only regular files are supported')
        content = base64.b64decode(item['data'], validate=True)
        if len(content) != item['size'] or hashlib.sha256(content).hexdigest() != item['sha256']:
            raise InvalidWorkspacePath('Recovery point checksum mismatch')
        if type(item['mode']) is not int or item['mode'] & ~0o755:
            raise InvalidWorkspacePath('Invalid file mode')
        total += len(content)
        if total > MAX_BYTES:
            raise UploadTooLarge('Recovery point exceeds size limit')
    if total != payload['bytes']:
        raise InvalidWorkspacePath('Recovery point size mismatch')


def receipt_value(target, request_id, digest, token):
    value = {'target':target, 'request_id':request_id, 'digest':digest}
    return {**value, 'signature':hmac.new(token.encode(), encode(value), hashlib.sha256).hexdigest()}


def restored(files, target, request_id, digest, token):
    parts = files._parts(target)
    if len(parts) != 1:
        raise InvalidWorkspacePath('Restore into a new root folder')
    try:
        with files._directory(parts) as folder:
            fd = os.open(RECEIPT, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=folder)
            with os.fdopen(fd,'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return False
                data = stream.read(2048)
            return hmac.compare_digest(data, encode(receipt_value(target, request_id, digest, token)))
    except (FileNotFoundError, NotADirectoryError):
        return False


def rename_noreplace(root, source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise OSError(errno.ENOSYS, 'Atomic no-replace rename unavailable')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(root, os.fsencode(source), root, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def restore(files, target, payload, request_id, token):
    validate(payload)
    digest = hashlib.sha256(encode(payload)).hexdigest()
    parts = files._parts(target)
    if len(parts) != 1:
        raise InvalidWorkspacePath('Restore into a new root folder')
    if restored(files, target, request_id, digest, token):
        return {'restored':True, 'path':target, 'digest':digest}
    with files._directory([]) as root:
        try:
            os.stat(target,dir_fd=root,follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(target)
        # A retry reclaims only this authenticated operation's unfinished staging directory.
        stage = files._temp_prefix+'restore-'+receipt_value(target,request_id,digest,token)['signature']
        def directory(parts):
            fd = os.open(stage,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
            try:
                for part in parts:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                    os.close(fd);fd=child
                return fd
            except BaseException:
                os.close(fd);raise
        def remove_tree(parent, name):
            fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            try:
                for item in os.scandir(fd):
                    if item.is_dir(follow_symlinks=False):remove_tree(fd,item.name)
                    else:os.unlink(item.name,dir_fd=fd)
            finally:os.close(fd)
            os.rmdir(name,dir_fd=parent)
        try:remove_tree(root,stage)
        except FileNotFoundError:pass
        os.mkdir(stage, mode=0o700, dir_fd=root)
        try:
            for path,item in sorted(payload['entries'].items(), key=lambda entry:(entry[0].count('/'),entry[0])):
                parts=files._parts(path);parent=directory(parts[:-1])
                try:
                    if item['kind']=='directory':os.mkdir(parts[-1],0o700,dir_fd=parent)
                    else:
                        fd=os.open(parts[-1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,item['mode']|0o600,dir_fd=parent)
                        with os.fdopen(fd,'wb') as stream:
                            stream.write(base64.b64decode(item['data']));stream.flush();os.fsync(stream.fileno())
                    os.fsync(parent)
                finally:os.close(parent)
            parent=directory([])
            try:
                fd=os.open(RECEIPT,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=parent)
                with os.fdopen(fd,'wb') as stream:
                    stream.write(encode(receipt_value(target,request_id,digest,token)));stream.flush();os.fsync(stream.fileno())
                os.fsync(parent)
            finally:os.close(parent)
            rename_noreplace(root, stage, target)
            os.fsync(root)
        finally:
            try:remove_tree(root,stage)
            except FileNotFoundError:pass
    return {'restored':True,'path':target,'digest':digest}
