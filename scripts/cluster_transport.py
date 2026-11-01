"""SSH subprocess transport for operator scripts; secrets stay in captured IO."""
import os
import shlex
import subprocess

HOST = os.environ.get("PLATFORM_SSH_HOST", "edge@100.92.98.122")
SOCKET = os.environ.get("PLATFORM_SSH_SOCKET", "/tmp/platform-control-edge-20261031.sock")


def remote(argv, stdin=None, redact=()):
    result = subprocess.run(["ssh", "-S", SOCKET, "-o", "BatchMode=yes", HOST, shlex.join(argv)], input=stdin, capture_output=True, text=True)
    if result.returncode:
        message = result.stderr[-4000:] + "\n" + "\n".join(line for line in result.stdout.splitlines() if line.startswith("RESULT:"))[-4000:]
        for value in redact:
            message = message.replace(value, "[redacted]")
        raise RuntimeError(f"Remote command failed ({result.returncode}): {message}")
    return result.stdout


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdin-file")
    parser.add_argument("--results-only", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    output = remote(args.command, Path(args.stdin_file).read_text() if args.stdin_file else None)
    if args.results_only:
        output = "\n".join(line for line in output.splitlines() if line.startswith("RESULT:"))
    print(output)
