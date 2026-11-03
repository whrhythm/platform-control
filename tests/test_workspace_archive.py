import io
import os
import tempfile
import zipfile

import pytest
from fastapi.testclient import TestClient

from control_plane.workspace_archive import archive
from control_plane.workspace_file_service import create_app
from control_plane.workspace_files import InvalidWorkspacePath, UploadTooLarge, WorkspaceFiles


@pytest.mark.skipif(os.name != 'posix', reason='Directory-FD exports require Linux')
def test_complete_zip_with_nested_empty_directories_and_no_link_following(tmp_path):
    project = tmp_path / '工程'
    (project / 'empty').mkdir(parents=True)
    (project / 'app.py').write_text('print("hello")')
    (project / 'outside').symlink_to('/etc/passwd')
    files = WorkspaceFiles(str(tmp_path))
    content, skipped = archive(files, '工程')
    with zipfile.ZipFile(io.BytesIO(content)) as result:
        assert result.read('工程/app.py') == b'print("hello")'
        assert '工程/empty/' in result.namelist() and '工程/outside' not in result.namelist()
    assert skipped == 1
    with tempfile.TemporaryFile() as target:
        assert archive(files, '工程', output=target)[0] is None
        assert zipfile.is_zipfile(target)
    with pytest.raises(InvalidWorkspacePath):
        archive(files, '../工程')
    with pytest.raises(UploadTooLarge):
        archive(files, '工程', max_source_bytes=1)
    with pytest.raises(UploadTooLarge):
        archive(files, '工程', max_entries=1)
    with TestClient(create_app(str(tmp_path), 's' * 40)) as client:
        assert client.get('/v1/archive?path=工程').status_code == 401
        for _ in range(2):
            response = client.get('/v1/archive?path=工程', headers={'Authorization': 'Bearer ' + 's' * 40})
            assert response.status_code == 200 and zipfile.is_zipfile(io.BytesIO(response.content))
