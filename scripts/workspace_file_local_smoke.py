"""Start a local-only disposable file API for manual/Postman testing.

The token is a public synthetic test value, and the workspace is a fresh
TemporaryDirectory. This helper is never suitable for a real user volume.
"""
import tempfile
from pathlib import Path
import sys
import uvicorn
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control_plane.workspace_file_service import create_app

with tempfile.TemporaryDirectory(prefix='codechat-files-smoke-') as root:
    app = create_app(root, 'synthetic-test-only-token-' + 'x' * 32, max_file_bytes=64)
    uvicorn.run(app, host='127.0.0.1', port=18763, access_log=False, log_level='warning')
