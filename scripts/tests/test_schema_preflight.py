import json
from types import SimpleNamespace
import pytest
from scripts.schema_preflight import check,MaintenanceRequired


def runner(old_head='e63b2a09c817',new_head='f74c3d10e928',previous=True,volumes=False,same=False):
    def run(args,**kwargs):
        if 'config' in args:value=json.dumps({'name':'isolated-test','services':{'api':{'image':'new-image'}}})
        elif 'ps' in args:value='a'*64 if previous else ''
        elif args[1:3]==['volume','ls']:value='isolated-test_mysql' if volumes else ''
        elif args[1:3]==['image','inspect']:value='sha256:'+'b'*64
        elif args[1:3]==['container','inspect']:value='sha256:'+('b' if same else 'c')*64
        elif args[1]=='run':
            assert '--network' in args and args[args.index('--network')+1]=='none'
            assert '--volume' not in args and '--env-file' not in args
            image=args[args.index('--entrypoint')+2]
            value=(new_head if image=='sha256:'+'b'*64 else old_head)+' (head)'
        else:raise AssertionError('Unexpected Docker action')
        return SimpleNamespace(returncode=0,stdout=value)
    return run


def test_daily_start_same_image_needs_no_migration():
    assert check(['compose'],runner=runner(same=True))['mode']=='same-image'


def test_new_code_with_same_schema_is_allowed():
    assert check(['compose'],runner=runner(old_head='f74c3d10e928'))['mode']=='same-schema'


def test_changed_schema_requires_reviewed_maintenance():
    with pytest.raises(MaintenanceRequired):check(['compose'],runner=runner())


def test_missing_container_with_business_volumes_cannot_guess_schema():
    with pytest.raises(MaintenanceRequired):check(['compose'],runner=runner(previous=False,volumes=True))
    assert check(['compose'],runner=runner(previous=False))['mode']=='new-installation'
