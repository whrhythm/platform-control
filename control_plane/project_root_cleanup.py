"""Inspect and retire one root created by a particular project request."""
import fcntl
from contextlib import ExitStack
from hashlib import sha256
import hmac
import json
import os
import re
import shutil
import stat

from .workspace_checkpoints import RECEIPT, encode, receipt_value, rename_noreplace
from .workspace_files import InvalidWorkspacePath, UploadTooLarge


EMPTY_ROOT = {'entries': {}, 'bytes': 0, 'excluded': []}
EMPTY_ROOT_DIGEST = sha256(encode(EMPTY_ROOT)).hexdigest()
ROOT_NAME = r'[a-z0-9][a-z0-9-]{0,79}'


def mount_id(fd):
    with open(f'/proc/self/fdinfo/{fd}') as info:
        return next(line for line in info if line.startswith('mnt_id:'))


def scope(files, path, project_id, token):
    if not re.fullmatch(ROOT_NAME, path):
        return None
    with files._directory([]) as root:
        return inspect_root(files, root, path, project_id, token)[0]


def inspect_root(files, root, path, project_id, token):
    digest = sha256()
    count = directories = size = 0

    def visit(fd, depth, mount):
        nonlocal count, directories, size
        if depth > 64:
            raise UploadTooLarge('Directory is too deep to remove')
        digest.update(encode(list(stat_values(os.fstat(fd)))))
        with os.scandir(fd) as entries:
            names = []
            for entry in entries:
                if len(names) >= 100_000:
                    raise UploadTooLarge('Too many entries to verify')
                names.append(entry.name)
        for name in sorted(names):
            if depth == 0 and name == RECEIPT:
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            digest.update(json.dumps([name, *stat_values(info)]).encode())
            if stat.S_ISDIR(info.st_mode):
                directories += 1
                with files._directory_handle(fd, name) as child:
                    if mount_id(child) != mount:
                        raise InvalidWorkspacePath('Directory contains a mount')
                    visit(child, depth + 1, mount)
            else:
                count += 1
                size += info.st_size
            if count + directories > 100_000:
                raise UploadTooLarge('Too many entries to verify')

    with ExitStack() as stack:
        try:
            folder = stack.enter_context(files._directory_handle(root, path))
            receipt = os.open(RECEIPT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            stream = stack.enter_context(os.fdopen(receipt, 'rb'))
        except (FileNotFoundError, NotADirectoryError):
            return None, None
        if not stat.S_ISREG(os.fstat(receipt).st_mode) or not hmac.compare_digest(
                stream.read(2048), encode(receipt_value(path, project_id, EMPTY_ROOT_DIGEST, token))):
            return None, None
        mount = mount_id(root)
        if mount_id(folder) != mount:
            raise InvalidWorkspacePath('Project root is a mount')
        identity = os.fstat(folder)
        visit(folder, 0, mount)
    return {'digest': digest.hexdigest(), 'files': count, 'directories': directories,
            'bytes': size, 'inode': identity.st_ino}, identity


def stat_values(info):
    return (info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def retire(files, path, project_id, expected, inode, token):
    if not re.fullmatch(ROOT_NAME, path) or not shutil.rmtree.avoids_symlink_attacks:
        raise InvalidWorkspacePath('Invalid project root')
    # A private staging name lets retries finish an interrupted recursive unlink
    # without ever touching a replacement created under the original path.
    signature = hmac.new(token.encode(), encode(['project-root-delete', path, project_id, expected]), sha256).hexdigest()
    stage = files._temp_prefix + 'project-delete-' + signature
    with files._directory([]) as root:
        fcntl.flock(root, fcntl.LOCK_EX)
        try:
            staged = os.stat(stage, dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.stat(path, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                return {'removed': True}
            current, identity = inspect_root(files, root, path, project_id, token)
            if not current or current['digest'] != expected or identity.st_ino != inode:
                return {'removed': False}
            rename_noreplace(root, path, stage)
            actual = os.stat(stage, dir_fd=root, follow_symlinks=False)
            if not os.path.samestat(identity, actual):
                rename_noreplace(root, stage, path)
                return {'removed': False}
            os.fsync(root)
        else:
            if staged.st_ino != inode or not stat.S_ISDIR(staged.st_mode):
                raise InvalidWorkspacePath('Unexpected staged directory')
        shutil.rmtree(stage, dir_fd=root)
        os.fsync(root)
    return {'removed': True}
