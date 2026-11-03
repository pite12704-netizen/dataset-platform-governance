from sqlalchemy import select
from app.models import ImageReviewImage,Membership
from app.image_reviews import create_batch
from test_image_reviews import setup_images

def setup_queue(app,client,actors,submit=True):
    owner,first,v,images,_=setup_images(app,client,actors,2)
    with app.state.db.Session() as db:
        for identifier in images:
            create_batch(db,v['id'],owner['id'],[{'asset_id':identifier,'person_key':'1','width':64,'height':80,
                'annotation':{'bbox':[1,2,20,30],'labels':{'hat':'hat','sleeve':'short_sleeves','upper_color':'red'}}}],{'origin':'test'})
        db.commit()
    if submit:assert client.post('/api/v1/image-reviews/versions/'+v['id']+'/submit',headers=owner['headers']).status_code==200
    return owner,first,actors['reader'],v

def next_image(client,actor,v,after=None):
    return client.post('/api/v1/review-workbench/'+v['id']+'/next',headers=actor['headers'],json={'after_image_id':after})

def test_cross_shard_queue_resume_concurrency_and_independent_confirmation(app,client,actors):
    owner,a,b,v=setup_queue(app,client,actors)
    inbox=client.get('/api/v1/review-workbench',headers=a['headers'])
    assert inbox.status_code==200,inbox.text
    assert len(inbox.json()['items'])==1 and inbox.json()['items'][0]['images']==2
    stats=client.get('/api/v1/review-workbench/'+v['id'],headers=a['headers'])
    assert stats.status_code==200,stats.text
    assert stats.json()['total_images']==2
    assert next_image(client,owner,v).status_code==403
    assert next_image(client,actors['stranger'],v).status_code==404
    first=next_image(client,a,v);assert first.status_code==200,first.text
    one=first.json()['image'];repeat=next_image(client,a,v).json()['image']
    assert one['id']==repeat['id'] and one['claim_token']==repeat['claim_token']
    two=next_image(client,b,v).json()['image'];assert two['id']!=one['id']
    path='/api/v1/image-reviews/images/'
    assert client.post(path+two['id']+'/release',headers=b['headers'],json={'claim_token':two['claim_token']}).status_code==200
    saved=client.put(path+one['id'],headers=a['headers'],json={'revision':one['revision'],'claim_token':one['claim_token'],
        'persons':[{'item_id':i['id'],'annotation':i['annotation']} for i in one['items']]})
    assert saved.status_code==200,saved.text
    assert client.post(path+one['id']+'/claim',headers=a['headers']).status_code==409
    second=next_image(client,a,v,one['id']).json()
    assert second['image']['id']==two['id'] and second['batch']['id']!=first.json()['batch']['id']
    stale=client.put(path+two['id'],headers=b['headers'],json={'revision':two['revision'],'claim_token':two['claim_token'],'persons':[]})
    assert stale.status_code==409
    review=next_image(client,b,v).json()['image'];assert review['id']==one['id'] and review['status']=='submitted'
    approved=client.post(path+review['id']+'/approve',headers=b['headers'],json={'revision':review['revision'],'claim_token':review['claim_token']})
    assert approved.status_code==200,approved.text
    waiting=next_image(client,b,v).json();assert waiting['image'] is None
    assert '其他人领取' in waiting['message']
    assert client.get('/api/v1/review-workbench/'+v['id'],headers=b['headers']).json()['counts']['approved']==1

def test_unsubmitted_private_version_is_not_exposed_to_review_staff(app,client,actors):
    owner,a,_,v=setup_queue(app,client,actors,submit=False)
    assert client.get('/api/v1/review-workbench',headers=a['headers']).json()['items']==[]
    assert client.get('/api/v1/review-workbench/'+v['id'],headers=a['headers']).status_code==404
    assert next_image(client,a,v).status_code==404
    assert client.get('/api/v1/review-workbench/'+v['id'],headers=owner['headers']).json()['draft_images']==2

def test_membership_revocation_blocks_resume_and_renew(app,client,actors):
    _,a,_,v=setup_queue(app,client,actors)
    image=next_image(client,a,v).json()['image']
    with app.state.db.Session() as db:
        row=db.scalar(select(Membership).where(Membership.user_id==a['id'],Membership.space_id!=a['space_id']))
        row.active=False;db.commit()
    assert next_image(client,a,v).status_code==404
    assert client.post('/api/v1/image-reviews/images/'+image['id']+'/renew',headers=a['headers'],json={'claim_token':image['claim_token']}).status_code==403

def test_skip_does_not_immediately_reclaim_the_only_available_image(app,client,actors):
    _,a,b,v=setup_queue(app,client,actors)
    one=next_image(client,a,v).json()['image'];next_image(client,b,v)
    assert client.post('/api/v1/image-reviews/images/'+one['id']+'/release',headers=a['headers'],json={'claim_token':one['claim_token']}).status_code==200
    assert next_image(client,a,v,one['id']).json()['image'] is None
    assert next_image(client,a,v).json()['image']['id']==one['id']
