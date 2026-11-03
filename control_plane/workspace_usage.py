"""Measure only a configured personal volume; no provider or tenant claims."""
from datetime import datetime, timezone
import os
import stat
import time


def measure_usage(root, *, max_entries=200000, max_seconds=10):
    started, count, used = time.monotonic(), 0, 0
    seen = set()
    with os.scandir(root):
        pass
    def scan_error(error):
        raise error
    for current, directories, files, directory in os.fwalk(root, follow_symlinks=False, onerror=scan_error):
        if current == root:
            directories[:] = [name for name in directories if name != 'lost+found']
        for name in files:
            count += 1
            if count > max_entries or time.monotonic() - started > max_seconds:
                raise ValueError('Usage scan exceeded supported limit')
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            identity = (info.st_dev, info.st_ino)
            if stat.S_ISREG(info.st_mode) and identity not in seen:
                used += info.st_size
                seen.add(identity)
        count += len(directories)
        if count > max_entries or time.monotonic() - started > max_seconds:
            raise ValueError('Usage scan exceeded supported limit')
    volume = os.statvfs(root)
    return {'used_bytes': used, 'physical_available_bytes': volume.f_bavail * volume.f_frsize,
            'physical_total_bytes': volume.f_blocks * volume.f_frsize,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'scope': 'personal_volume'}
