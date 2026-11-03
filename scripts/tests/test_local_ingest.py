"""本地目录准备和传输只用临时合成文件，不读取业务原图或访问网络。"""
import hashlib
import json

import pytest

from clients.ingest.exporter import SourceError
from clients.ingest.local_source import prepare_local
from clients.ingest.uploader import IngestFailure, local_file, send_file, upload


def prepare(root, output):
    return prepare_local(root, output, 'local-pictures', '合成测试来源', '测试许可', progress=lambda _: None)


def test_recursive_manifest_without_copy_and_resume_freezes_membership(tmp_path):
    root = tmp_path / '原图'
    (root / 'a').mkdir(parents=True)
    (root / 'b').mkdir()
    (root / 'a' / '同名.JPG').write_bytes(b'image-one')
    (root / 'b' / '同名.JPG').write_bytes(b'image-two')
    (root / 'notes.txt').write_text('not an image')
    output = tmp_path / 'manifest'
    summary = prepare(root, output)
    assert summary['complete'] and summary['files'] == 2 and summary['skipped_files'] == 1
    manifest = (output / 'manifest.jsonl').read_bytes()
    items = [json.loads(line) for line in manifest.splitlines()]
    assert len({item['external_id'] for item in items}) == 2
    assert [item['filename'] for item in items] == ['同名.JPG', '同名.JPG']
    assert all(item['source_group'] == '' for item in items)  # 不假定同目录就是同一主体。
    assert all(not path.suffix.lower() in {'.jpg', '.png', '.webp'} for path in output.rglob('*'))
    assert str(root) not in manifest.decode('utf-8')
    assert str(root) not in (output / 'summary.json').read_text(encoding='utf-8')
    (root / 'new.png').write_bytes(b'new-image')
    assert prepare(root, output)['files'] == 2
    assert (output / 'manifest.jsonl').read_bytes() == manifest


def test_changed_missing_and_invalid_files_never_report_complete(tmp_path, monkeypatch):
    from clients.ingest import local_source
    root = tmp_path / 'images'
    root.mkdir()
    (root / 'good.png').write_bytes(b'ok')
    (root / 'empty.png').write_bytes(b'')
    (root / 'large.png').write_bytes(b'too-big')
    monkeypatch.setattr(local_source, 'MAX_IMAGE', 4)
    output = tmp_path / 'manifest'
    result = prepare(root, output)
    assert not result['complete'] and result['failed'] == 2 and result['files'] == 1
    assert len(json.loads((output / 'errors.json').read_text(encoding='utf-8'))) == 2
    (root / 'good.png').unlink()
    result = prepare(root, output)
    assert not result['complete'] and result['failed'] == 3
    with pytest.raises(IngestFailure, match='导出尚不完整'):
        upload(output / 'manifest.jsonl', 'batch', None, input_dir=root)


def test_reprepare_detects_source_change_and_metadata_change(tmp_path):
    root = tmp_path / 'images'
    root.mkdir()
    (root / 'a.png').write_bytes(b'old')
    output = tmp_path / 'manifest'
    assert prepare(root, output)['complete']
    (root / 'a.png').write_bytes(b'new')
    result = prepare(root, output)
    assert not result['complete'] and result['failed'] == 1
    assert '发生变化' in (output / 'errors.json').read_text(encoding='utf-8')
    with pytest.raises(SourceError, match='配置或冻结清单已变化'):
        prepare_local(root, output, 'different', '来源', '许可')


@pytest.mark.parametrize('relative', ['../outside.png', '/absolute.png', 'C:/a.png', 'a/../b.png', r'a\b.png', 'a:stream.png'])
def test_client_rejects_unsafe_local_paths(tmp_path, relative):
    with pytest.raises(IngestFailure, match='路径超出'):
        local_file(tmp_path, {'local_path': relative})


def test_links_are_never_followed(tmp_path):
    root = tmp_path / 'images'
    root.mkdir()
    outside = tmp_path / 'outside.png'
    outside.write_bytes(b'private-data')
    try:
        (root / 'link.png').symlink_to(outside)
    except OSError:
        pytest.skip('OS does not permit test symlink creation')
    with pytest.raises(SourceError, match='链接'):
        prepare(root, tmp_path / 'manifest')
    with pytest.raises(IngestFailure, match='链接'):
        local_file(root, {'local_path': 'link.png'})


def test_overlapping_output_and_file_limit_rejected(tmp_path, monkeypatch):
    from clients.ingest import local_source
    root = tmp_path / 'images'
    root.mkdir()
    (root / 'a.png').write_bytes(b'a')
    (root / 'b.png').write_bytes(b'b')
    with pytest.raises(SourceError, match='互相包含'):
        prepare(root, root / 'output')
    monkeypatch.setattr(local_source, 'MAX_FILES', 1)
    with pytest.raises(SourceError, match='最多'):
        prepare(root, tmp_path / 'manifest')
    assert not (tmp_path / 'manifest' / 'summary.json').exists()


def test_local_upload_requires_explicit_input_and_checks_original_bytes(tmp_path):
    root = tmp_path / 'images'
    root.mkdir()
    (root / 'a.png').write_bytes(b'original')
    output = tmp_path / 'manifest'
    prepare(root, output)
    with pytest.raises(IngestFailure, match='显式指定'):
        upload(output / 'manifest.jsonl', 'batch', None)
    item = json.loads((output / 'manifest.jsonl').read_text(encoding='utf-8'))
    assert local_file(root, item) == root / 'a.png'
    (root / 'a.png').write_bytes(b'modified')
    with pytest.raises(IngestFailure, match='清单不一致'):
        local_file(root, item)


def test_mid_transfer_change_cannot_be_completed(tmp_path):
    path = tmp_path / 'image.png'
    path.write_bytes(b'original')
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    calls = []

    class Client:
        def request(self, method, url, **kwargs):
            calls.append((method, url))
            if url.endswith('/upload'):
                path.write_bytes(b'modified')
                return {'id': 'session', 'status': 'uploading', 'part_size': 3, 'parts': {}}
            return {}

    with pytest.raises(IngestFailure, match='上传期间'):
        send_file(Client(), 'batch', {'id': 'item', 'size': 8, 'sha256': expected}, path)
    assert not any(url.endswith('/complete') for _, url in calls)


@pytest.mark.parametrize('stored_hash', [None, hashlib.sha256(b'bad').hexdigest()])
def test_resume_rewrites_unverified_or_different_same_size_part(tmp_path, stored_hash):
    path = tmp_path / 'image.png'
    path.write_bytes(b'good')
    sent = []

    class Client:
        def request(self, method, url, **kwargs):
            if url.endswith('/upload'):
                return {'id': 'session', 'status': 'uploading', 'part_size': 4,
                        'parts': {'1': {'size': 4, 'etag': 'not-a-content-hash', 'sha256': stored_hash}}}
            if method == 'PUT':
                sent.append(kwargs['content'])
            return {}

    send_file(Client(), 'batch', {'id': 'item', 'size': 4, 'sha256': hashlib.sha256(b'good').hexdigest()}, path)
    assert sent == [b'good']
