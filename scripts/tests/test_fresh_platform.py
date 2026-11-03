from copy import deepcopy
from pathlib import Path

from scripts.fresh_platform import compose_model, env_text


def test_fresh_profile_never_reuses_business_volumes_or_old_application_environment(tmp_path):
    app={'image':'old','environment':{'OLD_VALUE':'must-not-copy'},'secrets':[{'source':'app_key','target':'app_key'}]}
    model={'services':{name:deepcopy(app) for name in ('api','worker','vision-worker','beat','migrate')},
           'volumes':{name:{'name':'old_'+name} for name in ('mysql','objects','redis','mail','clamav','caddy_data','caddy_config')},
           'secrets':{'app_key':{'file':'old-key'}},'networks':{'default':{'name':'old-default'}}}
    model['services'].update(web={'image':'old-web'},clamav={'image':'old-scanner'},mysql={},minio={},redis={},
        **{'storage-init':{'volumes':[{'source':'old-script','target':'/bootstrap/storage-init.sh'}]}})
    shared={name:'public_'+name for name in ('clamav','caddy_data','caddy_config')}
    result=compose_model(model,'dataset-platform-fresh-test',tmp_path,tmp_path/'service.env.local',shared)
    for name in ('mysql','objects','redis','mail'):
        assert result['volumes'][name]['name']=='dataset-platform-fresh-test_'+name
        assert not result['volumes'][name].get('external')
    assert result['networks']['default']['name']=='dataset-platform-fresh-test_default'
    assert all('OLD_VALUE' not in result['services'][name]['environment'] for name in ('api','worker','vision-worker','beat','migrate'))
    assert result['secrets']['smtp_password']['file']==str(tmp_path/'secrets/smtp_password')
    assert result['services']['mysql']['ports'][0]['host_ip']=='127.0.0.1'


def test_generated_settings_keep_json_and_boolean_types():
    result=env_text({'registration_mode':'open','require_verification':False,'cors_origins':['https://localhost:8443'],'smtp_host':''})
    assert 'DP_REQUIRE_VERIFICATION=false' in result and 'DP_SMTP_HOST=\n' in result
    assert 'DP_CORS_ORIGINS=["https://localhost:8443"]' in result
