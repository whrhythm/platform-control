from concurrent.futures import ThreadPoolExecutor
import os

import pytest

from control_plane.workspace_files import InvalidWorkspacePath, UploadTooLarge, WorkspaceFiles


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'user-a'
    root.mkdir()
    return WorkspaceFiles(str(root), max_file_bytes=32), root


def test_directories_upload_binary_read_and_explicit_overwrite(workspace):
    files, root = workspace
    files.create_directory('project')
    assert files.write_file('project/app.bin', [b'\x00abc', b'\xff']) == 5
    assert files.read_file('project/app.bin') == b'\x00abc\xff'
    assert files.list_directory() == [{'name': 'project', 'kind': 'directory', 'size': None}]
    assert files.list_directory('project') == [{'name': 'app.bin', 'kind': 'file', 'size': 5}]
    with pytest.raises(FileExistsError):
        files.write_file('project/app.bin', [b'unsafe-overwrite'])
    assert files.read_file('project/app.bin') == b'\x00abc\xff'
    files.write_file('project/app.bin', [b'updated'], overwrite=True)
    assert files.read_file('project/app.bin') == b'updated'
    assert not list(root.rglob('.codechat-upload-*'))


@pytest.mark.parametrize('path', ['/etc/passwd', '../other', 'a/../other', 'a//b', '.', 'a\\b', 'a/./b', 'a\x00b', '.codechat-upload-probe'])
def test_rejects_traversal_and_reserved_paths(workspace, path):
    files, _ = workspace
    with pytest.raises(InvalidWorkspacePath):
        files.write_file(path, [b'test'])


def test_symlinks_cannot_escape_or_replace_targets(workspace, tmp_path):
    files, root = workspace
    outside = tmp_path / 'user-b'
    outside.mkdir()
    (outside / 'secret').write_bytes(b'other-user')
    (root / 'link').symlink_to(outside, target_is_directory=True)
    (root / 'file-link').symlink_to(outside / 'secret')
    with pytest.raises(OSError):
        files.read_file('link/secret')
    with pytest.raises(OSError):
        files.write_file('link/new', [b'bad'])
    with pytest.raises(OSError):
        files.read_file('file-link')
    with pytest.raises(InvalidWorkspacePath):
        files.write_file('file-link', [b'bad'], overwrite=True)
    assert (outside / 'secret').read_bytes() == b'other-user'
    assert not (outside / 'new').exists()
    assert files.list_directory() == []


def test_hardlinks_to_other_paths_are_not_exposed(workspace, tmp_path):
    files, root = workspace
    other = tmp_path / 'outside'
    other.write_bytes(b'secret')
    os.link(other, root / 'linked')
    assert files.list_directory() == []
    with pytest.raises(InvalidWorkspacePath):
        files.read_file('linked')
    with pytest.raises(InvalidWorkspacePath):
        files.write_file('linked', [b'overwrite'], overwrite=True)
    assert other.read_bytes() == b'secret'


def test_overlimit_or_interrupted_upload_keeps_original_and_removes_temp(workspace, tmp_path):
    files, root = workspace
    files.write_file('keep', [b'original'])
    with pytest.raises(UploadTooLarge):
        files.write_file('keep', [b'x' * 33], overwrite=True)

    def interrupted():
        yield b'partial'
        raise ConnectionError('client disconnected')

    with pytest.raises(ConnectionError):
        files.write_file('keep', interrupted(), overwrite=True)
    assert files.read_file('keep') == b'original'
    assert not list(root.glob('.codechat-upload-*'))


def test_simultaneous_create_allows_exactly_one_complete_file(workspace):
    files, _ = workspace
    def create(payload):
        try:
            files.write_file('same', [payload])
            return True
        except FileExistsError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, [b'a' * 16, b'b' * 16]))
    assert sorted(results) == [False, True]
    assert files.read_file('same') in (b'a' * 16, b'b' * 16)


def test_sync_failure_before_publish_preserves_original(workspace, monkeypatch):
    files, root = workspace
    files.write_file('keep', [b'original'])
    def failing_sync(_descriptor):
        raise OSError('simulated storage failure')
    monkeypatch.setattr(os, 'fsync', failing_sync)
    with pytest.raises(OSError, match='simulated storage failure'):
        files.write_file('keep', [b'new'], overwrite=True)
    assert files.read_file('keep') == b'original'
    assert not list(root.glob('.codechat-upload-*'))


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX-only FIFO check')
def test_special_files_are_not_read(workspace):
    files, root = workspace
    os.mkfifo(root / 'pipe')
    with pytest.raises(InvalidWorkspacePath):
        files.read_file('pipe')
