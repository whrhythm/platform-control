"""Build Job init process: authenticated source download and safe extraction."""
import hashlib
import io
import json
import os
from pathlib import Path
import ssl
import tarfile

import httpx


def main():
    context = ssl.create_default_context(cafile='/credentials/ca.crt')
    token = Path('/credentials/workspace-token').read_text()
    with httpx.Client(verify=context, timeout=60, trust_env=False) as client:
        with client.stream('GET', os.environ['CONTEXT_URL'], params={'path': os.environ['PROJECT_PATH']},
                           headers={'Authorization': 'Bearer ' + token}) as response:
            if response.status_code != 200:
                raise RuntimeError('Project snapshot is unavailable')
            content = bytearray()
            for block in response.iter_bytes():
                content.extend(block)
                if len(content) > 64 * 1024 * 1024:
                    raise RuntimeError('Project exceeds the supported build context size')
    destination = Path('/context/src')
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(content), mode='r:') as archive:
        if any(not member.isfile() for member in archive.getmembers()):
            raise RuntimeError('Project snapshot contains unsupported entries')
        archive.extractall(destination, filter='data')
    if not (destination / 'Dockerfile').is_file():
        raise RuntimeError('A project Dockerfile is required')
    Path('/dev/termination-log').write_text(json.dumps({'source_sha256': hashlib.sha256(content).hexdigest()}))
    print('Project source snapshot prepared')


if __name__ == '__main__':
    main()
