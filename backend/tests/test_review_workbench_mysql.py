from test_review_mysql import mysql_case as mysql_case,concurrent_posts
from test_review_workbench import setup_queue

def test_real_mysql_cross_shard_and_same_user_claims(mysql_case):
    app,client,actors=mysql_case
    _,a,b,v=setup_queue(app,client,actors)
    path='/api/v1/review-workbench/'+v['id']+'/next'
    replies=concurrent_posts(app,[(path,a,{}),(path,b,{})])
    assert [r.status_code for r in replies]==[200,200],[r.text for r in replies]
    first,second=[r.json()['image'] for r in replies]
    assert first['id']!=second['id'] and first['batch_id']!=second['batch_id']
    repeated=concurrent_posts(app,[(path,a,{}),(path,a,{})])
    assert [r.status_code for r in repeated]==[200,200]
    assert {r.json()['image']['claim_token'] for r in repeated}=={first['claim_token']}
    assert {r.json()['image']['id'] for r in repeated}=={first['id']}
