"""Annotation review does not grant the right to approve a publication."""
from test_lifecycle import create_dataset,upload

def test_organization_publication_is_admin_only_and_revocation_is_immediate(client,actors):
    owner,reviewer,administrator,platform=[actors[x] for x in ['owner','reader','stranger','admin']]
    org=client.post('/api/v1/spaces',headers=owner['headers'],json={'name':'Publication duties'}).json()
    members='/api/v1/spaces/'+org['id']+'/members'
    for actor,role in [('reader','reviewer'),('stranger','admin')]:
        assert client.post(members,headers=owner['headers'],json={'email':actors[actor].get('email',actor+'@example.test'),'role':role}).status_code==200
    _,version=create_dataset(client,owner,visibility='organization',space=org['id'])
    upload(client,owner,version)
    response=client.post('/api/v1/versions/'+version['id']+'/submit',headers=owner['headers'])
    assert response.status_code==200,response.text
    publication=response.json();path='/api/v1/publications/'+publication['id']+'/decision'
    assert client.get('/api/v1/publications',headers=reviewer['headers']).json()==[]
    for actor in [reviewer,owner,platform]:
        assert client.post(path,headers=actor['headers'],json={'approve':True}).status_code==403
    records=client.get('/api/v1/publications',headers=administrator['headers']).json()
    assert len(records)==1 and records[0]['can_review']
    assert client.post(members,headers=owner['headers'],json={'email':administrator.get('email','stranger@example.test'),'role':'admin','active':False}).status_code==200
    assert client.get('/api/v1/publications',headers=administrator['headers']).json()==[]
    assert client.post(path,headers=administrator['headers'],json={'approve':True}).status_code==403
    assert client.post(members,headers=owner['headers'],json={'email':administrator.get('email','stranger@example.test'),'role':'admin','active':True}).status_code==200
    assert client.post(path,headers=administrator['headers'],json={'approve':False,'comment':'请补充说明'}).status_code==200

def test_organization_admin_cannot_approve_public_listing(client,actors):
    owner,administrator=actors['owner'],actors['stranger']
    org=client.post('/api/v1/spaces',headers=owner['headers'],json={'name':'Public listing boundary'}).json()
    assert client.post('/api/v1/spaces/'+org['id']+'/members',headers=owner['headers'],json={'email':administrator.get('email','stranger@example.test'),'role':'admin'}).status_code==200
    _,version=create_dataset(client,owner,visibility='public',space=org['id'])
    upload(client,owner,version)
    response=client.post('/api/v1/versions/'+version['id']+'/submit',headers=owner['headers'])
    assert response.status_code==200,response.text
    path='/api/v1/publications/'+response.json()['id']+'/decision'
    assert client.post(path,headers=administrator['headers'],json={'approve':True}).status_code==403
    assert client.post(path,headers=actors['admin']['headers'],json={'approve':True}).status_code==200
