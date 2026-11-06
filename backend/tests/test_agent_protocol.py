import json
import pytest
from app.agent_runtime import AgentStop, model_payload, run_loop
from app.agent_protocol import normalize_response, known_versions
from test_agent_runtime import V1, V2


def response(value):
    return {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(value)}}],
            'usage':{'total_tokens':20}}


def tools():
    from app.agent_tools import TOOL_CONFIG
    return [{'type':'function','function':{'name':n,'description':d,'parameters':s.model_json_schema()}}
            for n,(s,_,d) in TOOL_CONFIG.items() if n!='get_sample']


def test_json_schema_protocol_constrains_wire_format_without_native_tools(app):
    app.state.settings.agent_protocol='json-schema'
    app.state.settings.model_url='http://127.0.0.1:1234/v1';app.state.settings.model_name='synthetic'
    messages=[{'role':'system','content':'policy'},{'role':'user','content':json.dumps({'question':f'读取 {V2}',
        'selected_version_ids':[V1]})}]
    payload=model_payload(app.state.settings,messages,tools())
    assert 'tools' not in payload and 'tool_choice' not in payload
    assert payload['response_format']['json_schema']['strict']
    branches=payload['response_format']['json_schema']['schema']['anyOf']
    read=next(b for b in branches if b['properties'].get('name',{}).get('const')=='get_version')
    assert read['properties']['arguments']['properties']['version_id']['enum']==[V1,V2]
    assert messages[0]['content']=='policy'  # Call-specific adaptations never mutate stored history.


@pytest.mark.parametrize('value',[
    {'action':'tool','name':'execute_sql','arguments':{}},
    {'action':'approve','arguments':{}},
    {'action':'tool','name':'get_version','arguments':{},'approved':True},
    {'action':'tool','name':'get_version','arguments':'not-an-object'}])
def test_adapter_rejects_non_whitelisted_decisions(value):
    with pytest.raises(AgentStop,match='invalid_decision'):
        normalize_response(response(value),tools())


def test_real_adapter_still_uses_runtime_authorization_evidence_and_argument_validation(app,monkeypatch):
    cfg=app.state.settings;cfg.agent_protocol='json-schema';cfg.model_url='http://127.0.0.1:1234/v1';cfg.model_name='synthetic'
    replies=iter([response({'action':'tool','name':'get_version','arguments':{'version_id':V2}}),
        response({'action':'answer','answer':'版本号=7；文件数=83','version_ids':[V2]})])
    async def call(*args,**kwargs):return next(replies)
    monkeypatch.setattr('app.agent_runtime.async_completion',call)
    executed=[];authorized=[]
    def execute(name,args):
        executed.append((name,args.version_id))
        return {'version_id':V2,'number':7,'asset_count':83},[{'version_id':V2,'name':'synthetic'}],[{'version_id':V2}]
    result=run_loop(cfg,'读取版本详情',[],None,execute,lambda refs:authorized.append(True),lambda _:None)
    assert result['status']=='completed' and executed==[('get_version',V2)] and len(authorized)>=4
    replies=iter([response({'action':'tool','name':'get_version','arguments':{'version_id':V1,'sql':'select'}}),
        response({'action':'answer','answer':'ignored','version_ids':[V1]})])
    result=run_loop(cfg,'读取版本详情',[],None,execute,lambda refs:None,lambda _:None)
    assert result['status']=='degraded' and executed==[('get_version',V2)]


def test_history_and_description_strings_cannot_become_current_version_enums():
    data={'question':'搜索','prior_conversation_is_untrusted':[{'question':V1}],
          'description':json.dumps({'version_id':V2})}
    assert known_versions([{'role':'user','content':json.dumps(data)}])==[]


def test_heldout_questions_do_not_supply_expected_file_facts():
    from scripts.agent_eval_cases import heldout,grade
    for scenario in heldout(731846):
        for fact in scenario['facts']:
            if any(k in fact for k in ['文件数=','版本号=','检查数=','通过数=','失败数=','许可=']):
                assert fact not in scenario['question']
        result={'status':'completed','answer':'伪造文件数=10','citations':[]}
        assert not grade(result,scenario,[],scenario['_fixture']['versions'])['passed']


def test_json_release_gate_requires_heldout_and_matching_protocol(app,tmp_path):
    from datetime import datetime,timezone
    import hashlib
    from app.agent_release import validate,PROMPT_VERSION,REQUIRED_CASES
    path=tmp_path/'synthetic-gate-fixture.json'
    cfg=app.state.settings.model_copy(update={'model_name':'synthetic-model','agent_model_revision':'synthetic-weight',
        'agent_protocol':'json-schema','agent_release_evidence_file':path})
    flags=dict.fromkeys(['passed','workflow_passed','citation_passed','fact_assertions_passed','injection_safe'],True)
    report={'mode':'live-synthetic-tool-probe','model':cfg.model_name,'model_revision':cfg.agent_model_revision,
        'protocol':'json-schema','prompt_version':PROMPT_VERSION,'business_data_read':False,
        'generated_at':datetime.now(timezone.utc).isoformat(),'underlying_model_response_observed':True,'gate_passed':True,
        'scenarios':[{'id':name,**flags} for name in REQUIRED_CASES]}
    def save():
        report['total']=len(report['scenarios']);raw=json.dumps(report).encode();path.write_bytes(raw)
        cfg.agent_release_evidence_sha256=hashlib.sha256(raw).hexdigest()
    save()
    with pytest.raises(ValueError):validate(cfg)
    report['heldout_seed']=1234
    report['scenarios'] += [{'id':'heldout-'+name,**flags} for name in REQUIRED_CASES]
    save();validate(cfg)
    with pytest.raises(ValueError):validate(cfg.model_copy(update={'agent_protocol':'native'}))
    with pytest.raises(ValueError):validate(cfg.model_copy(update={'agent_release_evidence_sha256':'0'*64}))
