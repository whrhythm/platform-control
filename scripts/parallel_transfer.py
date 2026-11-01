"""Bounded parallel/resumable image upload across a high-latency SSH link."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

from cluster_transport import remote
from transfer_image import PROGRAM, REMOTE

LOCAL = Path(__file__).resolve().parents[1] / "release" / Path(REMOTE).name
CHUNK = 4 * 1024 * 1024


def upload(start, length):
    destination = REMOTE + ".part." + str(start)
    command = ["ssh", "-o", "ControlPath=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "edge@100.92.98.122", shlex.join(["python3", "-c", PROGRAM, destination])]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        state = json.loads(process.stdout.readline())
        if state["size"] > length:
            raise RuntimeError("Unexpected part length")
        with LOCAL.open("rb") as source:
            source.seek(start)
            digest = hashlib.sha256(source.read(state["size"]))
            if digest.hexdigest() != state["sha256"]:
                raise RuntimeError("Part prefix mismatch")
            remaining = length - state["size"]
            while remaining:
                data = source.read(min(65536, remaining))
                process.stdin.write(data)
                digest.update(data)
                remaining -= len(data)
            process.stdin.close()
            final = json.loads(process.stdout.readline())
            if process.wait() or final["size"] != length or final["sha256"] != digest.hexdigest():
                raise RuntimeError("Part verification failed")
            return {"path": destination, "start": start, "size": length, "sha256": digest.hexdigest()}
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def main():
    inspect = "import hashlib,json,os; p=" + repr(REMOTE) + "; h=hashlib.sha256(); f=open(p,'rb');\nwhile b:=f.read(1048576): h.update(b)\nprint(json.dumps({'size':f.tell(),'sha256':h.hexdigest()}))"
    state = json.loads(remote(["python3", "-c", inspect]))
    size = LOCAL.stat().st_size
    if state["size"] > size:
        raise RuntimeError("Remote image exceeds local size")
    with LOCAL.open("rb") as source:
        digest = hashlib.sha256(source.read(state["size"]))
        if digest.hexdigest() != state["sha256"]:
            raise RuntimeError("Remote image prefix mismatch")
    print(f"Verified existing image prefix {state['size']} / {size}; using four dedicated streams", flush=True)
    parts = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(upload, start, min(CHUNK, size-start)) for start in range(state["size"], size, CHUNK)]
        for future in as_completed(futures):
            part = future.result()
            parts.append(part)
            print(f"Verified part at {part['start']}: {len(parts)} / {len(futures)}", flush=True)
    # Append in order only after every part has been verified. Existing archives
    # are never replaced or truncated; a retry verifies the resulting prefix.
    assembly = """
import hashlib,json,sys,fcntl
metadata=json.loads(sys.stdin.read())
with open(metadata['path'],'r+b') as target:
    fcntl.flock(target,fcntl.LOCK_EX)
    target.seek(0,2)
    assert target.tell()==metadata['prefix']
    for part in sorted(metadata['parts'],key=lambda p:p['start']):
        assert target.tell()==part['start']
        with open(part['path'],'rb') as source:
            while chunk:=source.read(1048576): target.write(chunk)
    target.flush()
    target.seek(0)
    digest=hashlib.sha256()
    while chunk:=target.read(1048576): digest.update(chunk)
    print(json.dumps({'size':target.tell(),'sha256':digest.hexdigest()}))
"""
    final = json.loads(remote(["python3", "-c", assembly], json.dumps({"path": REMOTE, "prefix": state["size"], "parts": parts})))
    with LOCAL.open("rb") as source:
        expected = hashlib.sha256(source.read()).hexdigest()
    if final["size"] != size or final["sha256"] != expected:
        raise RuntimeError("Full image checksum mismatch")
    print("FULL IMAGE SHA256 VERIFIED: " + expected, flush=True)


if __name__ == "__main__":
    main()
