"""Local file primitives for a *pre-authorized* workspace directory.

This module does not authenticate users, resolve workspace ownership, start a
service, or enforce aggregate user quotas. Do not expose it to browsers until
those boundaries and the mounted volume lifecycle have been implemented.
Requires Linux/POSIX directory FDs and O_NOFOLLOW; fail closed elsewhere.
"""
from contextlib import contextmanager
import os
import secrets
import stat
from typing import Iterable


class InvalidWorkspacePath(ValueError):
    pass


class UploadTooLarge(ValueError):
    pass


class WorkspaceFiles:
    _temp_prefix = '.codechat-upload-'

    def __init__(self, root: str, *, max_file_bytes: int = 100 * 1024 * 1024, max_entries: int = 1000):
        if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_CLOEXEC'):
            raise RuntimeError('Secure workspace filesystem primitives are unavailable')
        if not os.path.isabs(root) or max_file_bytes < 1 or max_entries < 1:
            raise ValueError('An absolute workspace root and positive limits are required')
        self.root = root
        self.max_file_bytes = max_file_bytes
        self.max_entries = max_entries

    @classmethod
    def _parts(cls, path: str, *, allow_root: bool = False) -> list[str]:
        if path == '' and allow_root:
            return []
        if not isinstance(path, str) or not path or len(path.encode('utf-8')) > 4096:
            raise InvalidWorkspacePath('Invalid relative path')
        parts = path.split('/')
        if any(not part or part in ('.', '..') or '\\' in part or '\x00' in part or
               part.startswith(cls._temp_prefix) or len(part.encode('utf-8')) > 255 for part in parts):
            raise InvalidWorkspacePath('Invalid relative path')
        return parts

    @contextmanager
    def _directory(self, parts: list[str]):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open(self.root, flags)
        try:
            for part in parts:
                child = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = child
            yield current
        finally:
            os.close(current)

    def list_directory(self, path: str = '') -> list[dict]:
        entries = []
        with self._directory(self._parts(path, allow_root=True)) as directory:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if entry.name.startswith(self._temp_prefix):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        kind = 'file'
                    elif stat.S_ISDIR(info.st_mode):
                        kind = 'directory'
                    else:
                        continue
                    if len(entries) >= self.max_entries:
                        raise ValueError('Directory entry limit exceeded')
                    entries.append({'name': entry.name, 'kind': kind, 'size': info.st_size if kind == 'file' else None})
        return sorted(entries, key=lambda item: item['name'])

    def create_directory(self, path: str) -> None:
        parts = self._parts(path)
        with self._directory(parts[:-1]) as directory:
            os.mkdir(parts[-1], mode=0o700, dir_fd=directory)
            os.fsync(directory)

    def read_file(self, path: str) -> bytes:
        parts = self._parts(path)
        with self._directory(parts[:-1]) as directory:
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise InvalidWorkspacePath('Only singly linked regular files can be read')
                if info.st_size > self.max_file_bytes:
                    raise UploadTooLarge('File exceeds the configured transfer limit')
                with os.fdopen(descriptor, 'rb', closefd=False) as source:
                    data = source.read(self.max_file_bytes + 1)
                if len(data) > self.max_file_bytes:
                    raise UploadTooLarge('File exceeds the configured transfer limit')
                return data
            finally:
                os.close(descriptor)

    def write_file(self, path: str, chunks: Iterable[bytes], *, overwrite: bool = False) -> int:
        parts = self._parts(path)
        with self._directory(parts[:-1]) as directory:
            temporary = self._temp_prefix + secrets.token_hex(16)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
            size = 0
            try:
                with os.fdopen(descriptor, 'wb') as target:
                    for chunk in chunks:
                        if not isinstance(chunk, bytes):
                            raise TypeError('Upload chunks must be bytes')
                        size += len(chunk)
                        if size > self.max_file_bytes:
                            raise UploadTooLarge('File exceeds the configured transfer limit')
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if overwrite:
                    try:
                        existing = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        existing = None
                    if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
                        raise InvalidWorkspacePath('Only singly linked regular files can be overwritten')
                    os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
                else:
                    # link() atomically refuses a pre-existing destination, including a symlink.
                    os.link(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    os.unlink(temporary, dir_fd=directory)
                os.fsync(directory)
                return size
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
