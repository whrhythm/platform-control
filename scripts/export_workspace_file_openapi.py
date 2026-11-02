"""Export the internal service contract; no live service or credentials used."""
import json
from pathlib import Path
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from control_plane.workspace_file_service import create_app

with tempfile.TemporaryDirectory(prefix='workspace-schema-') as temporary:
    app = create_app(temporary, 'schema-only-placeholder-' + 'x' * 32)
    target = root / 'contracts/workspace-files.openapi.json'
    target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(target)
