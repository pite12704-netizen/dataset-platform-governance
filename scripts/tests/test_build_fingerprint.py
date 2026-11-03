import json
from types import SimpleNamespace
import pytest
from scripts.build_fingerprint import REQUIRED_FILES,fingerprint
from scripts.offline_delivery import verify_api_source


def source(tmp_path):
    for name in REQUIRED_FILES:
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic input\n')
    code=tmp_path/'backend/app/main.py';code.parent.mkdir(parents=True,exist_ok=True);code.write_text('version = 1')
    return code


def test_build_identity_tracks_code_but_not_private_runtime_material(tmp_path):
    code=source(tmp_path);before=fingerprint(tmp_path)
    (tmp_path/'.env').write_text('not read by fingerprint')
    assert fingerprint(tmp_path)==before
    code.write_text('version = 2');assert fingerprint(tmp_path)!=before


def test_same_schema_cannot_hide_a_stale_api_image(tmp_path):
    source(tmp_path);expected=fingerprint(tmp_path)
    def run(args,**kwargs):
        assert '--network' in args and 'none' in args and '--env-file' not in args
        return SimpleNamespace(returncode=0,stdout=json.dumps(expected))
    assert verify_api_source('synthetic-image',tmp_path,run)==expected['fingerprint']
    expected['fingerprint']='0'*64
    with pytest.raises(RuntimeError):verify_api_source('stale-image',tmp_path,run)
