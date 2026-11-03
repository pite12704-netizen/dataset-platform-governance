import json
from pathlib import Path
import pytest
from scripts.offline_delivery import verify,sha,source_files
from scripts.agent_eval_cases import cases,grade


def package(tmp_path):
    path=tmp_path/'images/0.tar';path.parent.mkdir();path.write_bytes(b'synthetic image placeholder')
    manifest={'format':1,'images':[{'path':'images/0.tar','id':'sha256:'+'a'*64}],
        'files':[{'path':'images/0.tar','size':path.stat().st_size,'sha256':sha(path)}]}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    return sha(tmp_path/'manifest.json')


def test_offline_checks_pinned_manifest_and_all_bytes(tmp_path):
    pinned=package(tmp_path);assert verify(tmp_path,pinned)['format']==1
    with pytest.raises(RuntimeError):verify(tmp_path,'0'*64)
    (tmp_path/'images/0.tar').write_bytes(b'tampered')
    with pytest.raises(RuntimeError):verify(tmp_path,pinned)


def test_offline_rejects_extra_files_and_path_traversal(tmp_path):
    pinned=package(tmp_path);(tmp_path/'extra.ps1').write_text('not listed')
    with pytest.raises(RuntimeError):verify(tmp_path,pinned)
    (tmp_path/'extra.ps1').unlink()
    manifest=json.loads((tmp_path/'manifest.json').read_text());manifest['files'][0]['path']='../outside'
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError):verify(tmp_path)


def test_offline_sources_never_include_runtime_secrets_or_datasets():
    paths=[str(relative).replace('\\','/') for _,relative in source_files()]
    assert 'scripts/enterprise.ps1' in paths and '.gitignore' in paths
    assert 'deployment/compose.local.yml' in paths
    assert all(('.local' not in path or path=='deployment/compose.local.yml') and '/secrets/' not in path and not path.startswith(('var/','usermanage/','.venv')) for path in paths)


def test_live_grading_rejects_wrong_facts_fake_citations_and_injection():
    scenario=cases('a'*32,'b'*32)[0]
    result={'answer':'文件数=10；许可=test-only','status':'completed','citations':[{'version_id':'a'*32}]}
    assert grade(result,scenario,['search_datasets','get_version'],['a'*32])['passed']
    assert not grade({**result,'answer':'文件数=20'},scenario,['search_datasets','get_version'],['a'*32])['passed']
    assert not grade({**result,'citations':[{'version_id':'bad'}]},scenario,['search_datasets','get_version'],['a'*32])['passed']
    assert not grade({**result,'answer':result['answer']+' SYNTHETIC_INJECTION_CANARY'},scenario,['search_datasets','get_version'],['a'*32])['passed']


def test_offline_preparation_creates_private_namespace_and_never_starts_services(tmp_path,monkeypatch):
    from copy import deepcopy
    from scripts import offline_setup,fresh_platform
    bundle=tmp_path/'bundle';bundle.mkdir();package(bundle)
    manifest=json.loads((bundle/'manifest.json').read_text())
    manifest['image_roles']={role:'sha256:'+'a'*64 for role in ('api','web','scanner','mysql','minio','mc','redis')}
    signatures=bundle/'signatures';signatures.mkdir()
    for name in ('main.cvd','daily.cvd'):
        path=signatures/name;path.write_bytes(b'synthetic signature fixture')
        manifest['files'].append({'path':'signatures/'+name,'size':path.stat().st_size,'sha256':sha(path)})
    (bundle/'manifest.json').write_text(json.dumps(manifest))
    project=tmp_path/'new-project';(project/'deployment').mkdir(parents=True)
    for name in ('storage-init.sh','bucket-policy.json'):(project/'deployment'/name).write_text('synthetic fixture')
    monkeypatch.setattr(offline_setup,'ROOT',project);monkeypatch.setattr(fresh_platform,'ROOT',project)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'private-data'))
    template={'image':'not-used','secrets':[]}
    model={'services':{name:deepcopy(template) for name in ('api','worker','vision-worker','beat','migrate','web','clamav','mysql','minio','redis')},
        'volumes':{name:{} for name in ('mysql','objects','redis','clamav','mail','caddy_data','caddy_config')},
        'networks':{'default':{}},'secrets':{name:{'file':'not-used'} for name in ('app_key','mysql_password','mysql_root_password','database_url','redis_url','redis_password','minio_root_user','minio_root_password','s3_access_key','s3_secret_key')}}
    model['services']['storage-init']={'image':'not-used','volumes':[{'target':'/bootstrap/storage-init.sh'},{'target':'/bootstrap/bucket-policy.json'}]}
    profile,roles,_=offline_setup.prepare(bundle,sha(bundle/'manifest.json'),compiler=lambda args:model)
    compiled=json.loads(Path(profile['compose_file']).read_text())
    assert compiled['networks']['default']['internal'] is False
    assert compiled['services']['clamav']['environment']['CLAMAV_NO_FRESHCLAMD']=='true'
    assert all(not definition.get('external') for definition in compiled['volumes'].values())
    assert all(definition.get('pull_policy')=='never' and 'build' not in definition for definition in compiled['services'].values())
    assert not (project/'var/run/active-deployment.json').exists()
    assert (Path(profile['directory'])/'storage-init.sh').is_file()
    assert Path(profile['secrets_dir']).is_dir() and profile['auto_model'] is False
