import json
from PIL import Image
from alembic import command
from alembic.config import Config
import pytest
from app.config import Settings
from app.db import Database
from app.vision import predict,fingerprint


def test_crop_pass_preserves_boxes_and_validates_each_person(app,monkeypatch):
    calls=[]
    def model(image,settings,prompt,schema,name,max_tokens=3500):
        calls.append(name)
        if name=='person_grounding':return {'persons':[{'bbox':[10,10,900,950],'labels':{'hat':'unclear','sleeve':'unclear','upper_color':'other_or_unclear'}}],'truncated':False},{'seconds':0}
        return {'hat':'hat','sleeve':'short_sleeves','upper_color':'red'},{'seconds':0}
    monkeypatch.setattr('app.vision.call_image',model)
    cfg=app.state.settings;old=fingerprint(cfg);cfg.vision_attribute_mode='crop-pass'
    assert fingerprint(cfg)!=old
    image=Image.new('RGB',(64,80),'blue')
    result,trace=predict(image,cfg);image.close()
    assert calls==['person_grounding','person_attributes']
    assert result.persons[0].bbox==[10,10,900,950] and result.persons[0].labels.hat=='hat'
    assert trace['attribute_mode']=='crop-pass'


@pytest.mark.parametrize('revision,format_version',[('c41f8d29a705',6),('d52a0f18b906',7),('e63b2a09c817',8),('f74c3d10e928',9),('a85d4e21fa39',10)])
def test_known_legacy_snapshots_reflect_actual_columns(tmp_path,revision,format_version):
    from scripts.platform_data import snapshot,upgrade_manifest
    cfg=Settings(_env_file=None,environment='test',database_url='sqlite:///'+str(tmp_path/'legacy.db'),secret_key='legacy-test-only-01234567890123456789')
    config=Config('backend/alembic.ini');config.attributes['settings']=cfg;command.upgrade(config,revision)
    db=Database(cfg.database_url,cfg)
    try:
        manifest=snapshot(db)
        assert manifest['schema_version']==format_version
        restored=upgrade_manifest(manifest)
        assert restored['schema_version']==13 and 'thumbnails' in restored['tables'] and 'job_payloads' in restored['tables']
    finally:db.engine.dispose()
