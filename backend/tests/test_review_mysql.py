"""真实 MySQL 并发回归；仅允许 scripts/test_review_concurrency.py 创建的隔离库。"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.auth import add_user
from app.config import Settings
from app.main import create_app
from app.models import ImageReviewEvent
from app.schemas import Register
from test_image_reviews import submitted_review


@pytest.fixture
def mysql_case(tmp_path):
    url = os.environ.get('DP_REVIEW_TEST_URL')
    if not url:
        pytest.skip('运行 scripts/test_review_concurrency.py 启用真实 MySQL 并发测试')
    parsed = make_url(url)
    assert parsed.database == 'dataset_platform_review_concurrency' and parsed.username == 'dp_test'
    settings = Settings(_env_file=None, environment='test', deployment='public',
        secret_key='mysql-review-test-key-01234567890123456789', database_url=url,
        storage_backend='local', storage_root=tmp_path / 'objects', task_mode='inline', scan_mode='development',
        require_admin_mfa=False, require_verification=False, mail_spool=tmp_path / 'mail',
        rate_limit_per_minute=10000, login_limit_per_minute=10000)
    app = create_app(settings)
    with TestClient(app) as client:
        actors = {}
        suffix = uuid.uuid4().hex
        for name in ['owner', 'reader', 'stranger', 'admin']:
            email = f'{name}.{suffix}@example.test'
            with app.state.db.Session() as db:
                user = add_user(db, Register(email=email, name=name, password='A-long-test-password!'),
                                settings, verified=True, admin=False)
                db.commit()
                identifier = user.id
            token = client.post('/api/v1/auth/login', json={'email': email, 'password': 'A-long-test-password!'}).json()['access_token']
            headers = {'Authorization': 'Bearer ' + token}
            client.cookies.clear()
            spaces = client.get('/api/v1/spaces', headers=headers).json()
            actors[name] = {'id': identifier, 'email': email, 'headers': headers, 'space_id': spaces[0]['id']}
        yield app, client, actors
    app.state.db.engine.dispose()


def concurrent_posts(app, calls):
    gate = Barrier(len(calls))
    def run(call):
        path, actor, data = call
        # 每个 TestClient 有独立事件循环；异步连接池不能跨循环共享应用实例。
        with TestClient(create_app(app.state.settings)) as thread_client:
            gate.wait(timeout=10)
            return thread_client.post(path, headers=actor['headers'], json=data)
    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        return list(executor.map(run, calls))


def test_mysql_simultaneous_claims_and_last_items_complete_batch(mysql_case):
    app, client, actors = mysql_case
    owner, first, second, batch, _ = submitted_review(app, client, actors)
    root = f"/api/v1/image-reviews/{batch['id']}"
    replies = concurrent_posts(app, [(root + '/claim-next', first, {}), (root + '/claim-next', second, {})])
    assert [r.status_code for r in replies] == [200, 200], [r.text for r in replies]
    claims = [r.json() for r in replies]
    assert claims[0]['item']['id'] != claims[1]['item']['id']
    replies = concurrent_posts(app, [(f"/api/v1/image-reviews/items/{c['item']['id']}/transition", actor,
        {'revision': 1, 'claim_token': c['claim_token'], 'action': 'approve'}) for c, actor in zip(claims, [first, second])])
    assert [r.status_code for r in replies] == [200, 200], [r.text for r in replies]
    result = client.get(root, headers=owner['headers']).json()
    assert result['status'] == 'completed' and result['counts']['approved'] == 2
    with app.state.db.Session() as db:
        events = db.scalars(select(ImageReviewEvent).where(
            ImageReviewEvent.item_id.in_([c['item']['id'] for c in claims]), ImageReviewEvent.action == 'approve')).all()
        assert len(events) == 2


def test_mysql_same_item_has_one_claimant_and_duplicate_approval_is_blocked(mysql_case):
    app, client, actors = mysql_case
    _, first, second, batch, _ = submitted_review(app, client, actors, 1)
    item = batch['items'][0]
    root = f"/api/v1/image-reviews/items/{item['id']}"
    replies = concurrent_posts(app, [(root + '/claim', first, {}), (root + '/claim', second, {})])
    assert sorted(r.status_code for r in replies) == [200, 409], [r.text for r in replies]
    winner_index = next(i for i, r in enumerate(replies) if r.status_code == 200)
    token, winner = replies[winner_index].json()['claim_token'], [first, second][winner_index]
    data = {'revision': 1, 'claim_token': token, 'action': 'approve'}
    replies = concurrent_posts(app, [(root + '/transition', winner, data)] * 2)
    assert sorted(r.status_code for r in replies) == [200, 409], [r.text for r in replies]
    with app.state.db.Session() as db:
        events = db.scalars(select(ImageReviewEvent).where(ImageReviewEvent.item_id == item['id'],
                                                          ImageReviewEvent.action == 'approve')).all()
        assert len(events) == 1
