from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.image_index import HammingIndex
from app.governance import validate
from app.models import User,Version,Job
from app.schemas import JobInput
from test_lifecycle import create_dataset


def test_search_budget_is_global_and_cancellation_pulses_propagate():
    tree=HammingIndex()
    for n in range(300):tree.add(n,n)
    list(tree.search(0,0,max_comparisons=300))
    with pytest.raises(RuntimeError,match='计算预算'):list(tree.search(0,64,max_comparisons=300))
    other=HammingIndex()
    for n in range(300):other.add(n,n)
    def cancel():raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError,match='cancelled'):list(other.search(0,64,heartbeat=cancel))


def test_capacity_reports_actual_count_and_preserves_owner_boundary(client,actors,app):
    owner=actors['owner'];_,version=create_dataset(client,owner)
    images=[SimpleNamespace(id=str(n),filename=f'{n}.jpg',size=1) for n in range(2001)]
    data=JobInput(version_id=version['id'],kind='dedup',idempotency_key='capacity')
    with app.state.db.Session() as db:
        user=db.get(User,owner['id']);v=db.get(Version,version['id'])
        app.state.settings.governance_max_images=2000
        with pytest.raises(HTTPException) as error:validate(db,user,v,images,data,app.state.settings)
        assert error.value.status_code==413 and '2001' in error.value.detail and '2000' in error.value.detail
        app.state.settings.governance_max_images=20000
        validate(db,user,v,images,data,app.state.settings)
        with pytest.raises(HTTPException) as denied:validate(db,db.get(User,actors['stranger']['id']),v,images,data,app.state.settings)
        assert denied.value.status_code==403


def test_compact_governance_filter_precedes_paging_and_never_widens_identity(client,actors,app):
    owner=actors['owner'];_,v=create_dataset(client,owner);_,other=create_dataset(client,owner)
    with app.state.db.Session() as db:
        for n,(version,kind) in enumerate([(v,'dedup'),(other,'dedup'),(v,'thumbnail')]):
            db.add(Job(space_id=owner['space_id'],user_id=owner['id'],version_id=version['id'],kind=kind,
                parameters={},result={},status='queued',idempotency_key=f'filter-{n}'))
        db.commit()
    path=f"/api/v1/jobs?compact=true&limit=1&version_id={v['id']}&governance_only=true"
    response=client.get(path,headers=owner['headers']);assert response.status_code==200
    assert len(response.json()['items'])==1 and response.json()['items'][0]['kind']=='dedup'
    assert not response.json()['has_more']
    assert client.get(path,headers=actors['stranger']['headers']).json()['items']==[]
