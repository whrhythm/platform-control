"""Bounded, link-free source snapshots from the existing personal filesystem."""
import io
import os
import tarfile

from .workspace_files import WorkspaceFiles, UploadTooLarge


def snapshot(files: WorkspaceFiles, path: str, *, max_bytes=48 * 1024 * 1024, max_files=2000) -> bytes:
    base = files._parts(path)
    pending = [('', base)]
    total, count = 0, 0
    output = io.BytesIO()
    excluded = {'.git', '.cache', 'node_modules', '.venv', '__pycache__', '.pytest_cache'}
    with tarfile.open(fileobj=output, mode='w') as archive:
        while pending:
            prefix, parts = pending.pop()
            for entry in files.list_directory('/'.join(parts)):
                if entry['name'] in excluded:
                    continue
                relative = prefix + entry['name']
                full = parts + [entry['name']]
                if entry['kind'] == 'directory':
                    if len(full) > 40:
                        raise ValueError('Project nesting exceeds the supported depth')
                    pending.append((relative + '/', full))
                    if len(pending) > max_files:
                        raise UploadTooLarge('Project contains too many directories')
                    continue
                count += 1
                total += entry['size']
                if count > max_files or total > max_bytes:
                    raise UploadTooLarge('Project snapshot exceeds the build limit')
                content = files.read_file('/'.join(full))
                total += len(content) - entry['size']
                if total > max_bytes:
                    raise UploadTooLarge('Project changed beyond the build limit')
                with files._directory(parts) as directory:
                    mode = os.stat(entry['name'], dir_fd=directory, follow_symlinks=False).st_mode & 0o755
                item = tarfile.TarInfo(relative)
                item.size, item.mode = len(content), mode
                archive.addfile(item, io.BytesIO(content))
    if output.tell() > 64 * 1024 * 1024:
        raise UploadTooLarge('Project snapshot exceeds the transfer limit')
    return output.getvalue()
