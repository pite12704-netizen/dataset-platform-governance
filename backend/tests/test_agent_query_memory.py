import json
from app.agent_query_memory import refine, evidence_matches
from app.models import Dataset
from test_lifecycle import create_dataset
from test_retrieval import retrieval, drain


def ask(client, actor, message, conversation=None):
    r=client.post('/api/v1/agent/chat',headers=actor['headers'],json={
        'message':message,'intent':'semantic_search','conversation_id':conversation})
    assert r.status_code==200
    return r.json()


def test_requirements_survive_many_turns_without_pins():
    state=refine(None,'我要找一些老人的数据','catalog')
    for _ in range(40):state=refine(state,'继续找','catalog')
    state=refine(state,'只要图片','catalog')
    state=refine(state,'还要戴帽子','catalog')
    assert state['concepts']==['elderly','hat'] and state['modality']=='image'
    assert state['turns_folded']==43 and len(json.dumps(state,ensure_ascii=False).encode())<8192
    state=refine(state,'不要求帽子了','catalog')
    assert state['concepts']==['elderly'] and '帽' not in state['effective_query']
    assert refine(state,'重新找儿童文本数据','catalog')['concepts']==['child']


def test_negative_conditions_are_not_silently_discarded():
    state=refine(refine(None,'老人数据','catalog'),'不要文本','catalog')
    assert state['excluded_modalities']==['text']
    negative=refine(state,'不要戴帽子的','catalog')
    assert 'no_hat' in negative['concepts'] and not negative['unresolved']
    assert not evidence_matches('不包含老人，没有帽子',['elderly','hat'])


def test_male_no_hat_is_parsed_without_clarification():
    s=refine(None,'我需要有男人的数据集，不戴帽子','catalog')
    assert s['concepts']==['male','no_hat'] and not s['unresolved']
    assert refine(None,'female without a hat','catalog')['concepts']==['female','no_hat']
    assert not evidence_matches('female person',['male'])


def test_reviewed_attributes_must_match_same_person(retrieval,client,actors):
    from app.models import Asset,ImageReviewBatch,ImageReviewItem,ImageReviewImage
    owner=actors['owner'];ds,v=create_dataset(client,owner,modality='image')
    with retrieval.state.db.Session() as db:
        batch=ImageReviewBatch(version_id=v['id'],creator_id=owner['id'],status='submitted',provenance={})
        db.add(batch);db.flush()
        for n,labels in enumerate([
            [{'gender':'male','hat':'hat'},{'gender':'female','hat':'no_hat'}],
            [{'gender':'male','hat':'unclear'}],
            [{'gender':'male','hat':'no_hat'}],
            [{'gender':'male','hat':'no_hat'}],
        ]):
            asset=Asset(version_id=v['id'],filename=f'{n}.png',object_key=f'synthetic/{n}',size=10,
                sha256=str(n)*64,media_type='image/png',status='clean');db.add(asset);db.flush()
            db.add(ImageReviewImage(batch_id=batch.id,asset_id=asset.id,width=10,height=10,
                status='pending' if n==2 else 'approved',editor_id=owner['id'],reviewer_id=actors['admin']['id']))
            for k,label in enumerate(labels):
                db.add(ImageReviewItem(batch_id=batch.id,asset_id=asset.id,person_key=str(k),width=10,height=10,
                    predicted={},annotation={'category':'person','bbox':[0,0,5,5],'labels':label},status='approved',
                    editor_id=owner['id'],reviewer_id=actors['admin']['id']))
        db.commit()
    result=ask(client,owner,'我需要有男人的数据集，不戴帽子')
    assert result['citations'][0]['version_id']==v['id']
    assert result['citations'][0]['matched_images']==1 and result['citations'][0]['content_evidence']
    assert result['access_requirements'][0]['content'] is True


def test_unmodeled_conditions_have_a_visible_budget_and_reset():
    state=refine(None,'寻找老人的数据','catalog')
    for i in range(5):state=refine(state,'另外要求特殊场景'+str(i),'catalog')
    assert state['needs_history_clarification'] and len(state['additional_requirements'])==4
    assert not refine(state,'重新找儿童数据','catalog')['needs_history_clarification']


def test_semantic_candidates_need_actual_metadata_evidence(retrieval,client,actors):
    owner=actors['owner']; ds,v=create_dataset(client,owner,modality='image')
    drain(retrieval)
    first=ask(client,owner,'我要找一些老人的数据')
    assert first['citations']==[] and first['query_memory']['concepts']==['elderly']
    with retrieval.state.db.Session() as db:
        db.get(Dataset,ds['id']).description='老人图像，包含戴帽子的老年人，用于目标检测'
        db.commit()
    drain(retrieval)
    second=ask(client,owner,'只要图片',first['conversation_id'])
    assert second['citations'][0]['version_id']==v['id']
    third=ask(client,owner,'还要戴帽子',first['conversation_id'])
    assert third['query_memory']['concepts']==['elderly','hat']
    assert third['tool']=='semantic_search'
    pin=client.post('/api/v1/agent/conversations/'+first['conversation_id']+'/runs/'+third['run_id']+'/pin',
        headers=owner['headers'],json={'pinned':True})
    assert pin.status_code==200
    denied=client.post('/api/v1/agent/chat',headers=actors['stranger']['headers'],json={
        'message':'继续找','intent':'semantic_search','conversation_id':first['conversation_id']})
    assert denied.status_code==404
    fresh=ask(client,owner,'查找儿童数据')
    assert fresh['query_memory']['concepts']==['child'] and fresh['query_memory']['turns_folded']==1
