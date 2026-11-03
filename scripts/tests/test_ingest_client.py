"""客户工具的来源失败、快照恢复和本地清单边界。"""
import hashlib
import io
import json

import pytest
from sqlalchemy import create_engine, text

from clients.ingest.exporter import env_value, export, SourceError
from clients.ingest.uploader import IngestFailure, PlatformClient, local_file, upload


class Source:
    def __init__(self):
        self.objects = {}
    def head_object(self, Bucket, Key, **kwargs):
        return {'ContentLength': len(self.objects[Key]), 'ETag': 'frozen'}
    def get_object(self, Bucket, Key, IfMatch, **kwargs):
        assert IfMatch == 'frozen'
        return {'Body': io.BytesIO(self.objects[Key])}


def test_missing_source_is_not_silent_success_and_resume_keeps_snapshot(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'source.db'))
    with engine.begin() as db:
        db.execute(text('CREATE TABLE photos(id INTEGER PRIMARY KEY,object_key TEXT)'))
        db.execute(text("INSERT INTO photos VALUES (1,'a.png')"))
    config = {'source_id': 'customer', 'database': {'table': 'photos', 'columns': {'id': 'id', 'key': 'object_key'}},
              'storage': {'bucket': 'source'}}
    source = Source(); output = tmp_path/'output'
    assert export(config, output, engine, source)['complete'] is False
    source.objects['a.png'] = b'isolated-source-image-bytes'
    result = export(config, output, engine, source)
    assert result['complete'] and result['files'] == 1
    changed = {**config, 'source': 'changed metadata'}
    with pytest.raises(SourceError):
        export(changed, output, engine, source)
    engine.dispose()


def test_upload_rejects_tampered_manifest_and_path_escape(tmp_path):
    manifest = tmp_path/'manifest.jsonl'; manifest.write_text('changed', encoding='utf-8')
    (tmp_path/'summary.json').write_text(json.dumps({'complete': True, 'files': 1, 'manifest_sha256': 'a'*64}))
    with pytest.raises(IngestFailure, match='清单已变化'):
        upload(manifest, 'batch', None)
    with pytest.raises(IngestFailure, match='路径超出'):
        local_file(tmp_path.resolve(), {'local_path': '../outside.png'})
    with pytest.raises(ValueError):
        PlatformClient('http://untrusted.example', 'synthetic-test-token')


def test_source_credentials_can_be_read_from_explicit_local_secret_files(tmp_path, monkeypatch):
    path = tmp_path/'synthetic-secret'; path.write_text('synthetic-only', encoding='utf-8')
    monkeypatch.setenv('CUSTOMER_TEST_SECRET_FILE', str(path))
    assert env_value('CUSTOMER_TEST_SECRET') == 'synthetic-only'
