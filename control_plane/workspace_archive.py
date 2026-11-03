"""Bounded ZIP export of owner files; links are never followed."""
import os
import stat
import tempfile
import time
import zipfile
from contextlib import nullcontext

from .workspace_files import FileVersionConflict, InvalidWorkspacePath, UploadTooLarge


def archive(files, path, *, max_bytes=32 * 1024 ** 2, max_source_bytes=256 * 1024 ** 2,
            max_entries=10000, max_seconds=30, output=None):
    parts = files._parts(path)
    count = total = skipped = 0
    deadline = time.monotonic() + max_seconds
    external = output is not None
    with (nullcontext(output) if external else tempfile.TemporaryFile()) as output:
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=2) as result:
            def walk(directory, relative, depth):
                nonlocal count, total, skipped
                if depth > 64:
                    raise UploadTooLarge('Archive directory depth exceeded')
                result.writestr(relative + '/', b'')
                with os.scandir(directory) as entries:
                    for entry in entries:
                        count += 1
                        if count > max_entries or time.monotonic() > deadline:
                            raise UploadTooLarge('Archive export limit exceeded')
                        try:
                            files._parts(entry.name)
                        except InvalidWorkspacePath:
                            skipped += 1
                            continue
                        info = entry.stat(follow_symlinks=False)
                        name = relative + '/' + entry.name
                        if stat.S_ISDIR(info.st_mode):
                            child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                            try:
                                walk(child, name, depth + 1)
                            finally:
                                os.close(child)
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            descriptor = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
                            try:
                                before = os.fstat(descriptor)
                                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                                    raise InvalidWorkspacePath('File identity changed')
                                with os.fdopen(descriptor, 'rb', closefd=False) as source, result.open(name, 'w') as target:
                                    while data := source.read(65536):
                                        total += len(data)
                                        if total > max_source_bytes or output.tell() > max_bytes or time.monotonic() > deadline:
                                            raise UploadTooLarge('Archive export limit exceeded')
                                        target.write(data)
                                if files.version(before) != files.version(os.fstat(descriptor)):
                                    raise FileVersionConflict('File changed during export')
                            finally:
                                os.close(descriptor)
                        else:
                            skipped += 1
                        if output.tell() > max_bytes:
                            raise UploadTooLarge('Archive export limit exceeded')
            with files._directory(parts) as directory:
                walk(directory, parts[-1], 0)
        if output.tell() > max_bytes:
            raise UploadTooLarge('Archive export limit exceeded')
        output.seek(0)
        return (None if external else output.read(max_bytes + 1)), skipped
