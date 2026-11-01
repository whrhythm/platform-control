"""Resume an image archive over a dedicated SSH stream, verifying its prefix."""
import argparse
import hashlib
import json
import shlex
import subprocess
import time
from pathlib import Path

REMOTE = "/home/edge/platform-releases/20261031/platform-control-0.1.0-20261031.1.tar.gz"
PROGRAM = r'''
import fcntl,hashlib,json,os,sys
path=sys.argv[1]
with open(path,'a+b') as target:
    fcntl.flock(target,fcntl.LOCK_EX)
    target.seek(0)
    digest=hashlib.sha256()
    while chunk:=target.read(1024*1024): digest.update(chunk)
    print(json.dumps({'size':target.tell(),'sha256':digest.hexdigest()}),flush=True)
    while chunk:=sys.stdin.buffer.read(65536):
        target.write(chunk)
        target.flush()
        digest.update(chunk)
    print(json.dumps({'size':target.tell(),'sha256':digest.hexdigest()}),flush=True)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-bytes", type=int)
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / "release" / Path(REMOTE).name
    command = ["ssh", "-o", "ControlPath=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "edge@100.92.98.122", shlex.join(["python3", "-c", PROGRAM, REMOTE])]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=False)
    try:
        state = json.loads(process.stdout.readline())
        offset = state["size"]
        if offset > path.stat().st_size:
            raise RuntimeError("Remote archive is unexpectedly larger")
        with path.open("rb") as source:
            digest = hashlib.sha256()
            remaining = offset
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != state["sha256"]:
                raise RuntimeError("Remote prefix does not match; refusing to append")
            print(f"Resume verified at {offset} / {path.stat().st_size} bytes", flush=True)
            sent = 0
            started = time.monotonic()
            while chunk := source.read(min(65536, args.max_bytes - sent) if args.max_bytes else 65536):
                process.stdin.write(chunk)
                digest.update(chunk)
                sent += len(chunk)
                if sent % (1024 * 1024) == 0:
                    print(f"Transferred {offset + sent} bytes; {sent / max(time.monotonic()-started,0.01) / 1024:.0f} KiB/s", flush=True)
                if args.max_bytes and sent >= args.max_bytes:
                    break
            process.stdin.close()
            final = json.loads(process.stdout.readline())
            if process.wait() != 0 or final["size"] != offset + sent or final["sha256"] != digest.hexdigest():
                raise RuntimeError("Archive verification failed")
            print("Remote prefix SHA256 verified: " + final["sha256"], flush=True)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
