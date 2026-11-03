"""权限过滤必须早于数据库分页；目录查询次数不随本页卡片数线性增加。"""
from sqlalchemy import event, select

from app.datasets import catalog_query
from app.models import Dataset, Membership, Project, ProjectMember, User, Version
from app.policy import discover
from test_lifecycle import create_dataset


def test_catalog_sql_visibility_matches_policy_and_project_removal(app, client, actors):
    owner, reader = actors['owner'], actors['reader']
    org = client.post('/api/v1/spaces', headers=owner['headers'], json={'name': '目录权限回归'}).json()
    client.post(f"/api/v1/spaces/{org['id']}/members", headers=owner['headers'],
        json={'email': reader.get('email', 'reader@example.test'), 'role': 'member'})
    project = client.post(f"/api/v1/spaces/{org['id']}/projects", headers=owner['headers'], json={'name': '检索项目'}).json()
    client.post(f"/api/v1/spaces/{org['id']}/projects/{project['id']}/members", headers=owner['headers'],
        json={'user_id': reader['id'], 'can_apply': True})
    with app.state.db.Session() as db:
        for visibility, status, users, projects in [
            ('public', 'active', [], []), ('public', 'blocked', [], []),
            ('organization', 'active', [], []), ('selected', 'active', [reader['id']], []),
            ('selected', 'active', [reader['id'] + 'x'], []),
            ('selected', 'active', [], [project['id']]), ('private', 'active', [], [])]:
            ds = Dataset(space_id=org['id'], owner_id=owner['id'], name=visibility,
                visibility=visibility, status=status, audience_users=users, audience_projects=projects)
            db.add(ds); db.flush()
            db.add(Version(dataset_id=ds.id, number=1, creator_id=owner['id'], status='published', license='test', source='test'))
        db.commit()
        for private in [False, True]:
            app.state.settings.deployment = 'private' if private else 'public'
            for actor in [None, owner, reader, actors['stranger']]:
                user = db.get(User, actor['id']) if actor else None
                expected = {ds.id for ds in db.scalars(select(Dataset)) if discover(db, user, ds, app.state.settings)}
                actual = {ds.id for ds in db.scalars(catalog_query(db, user, app.state.settings))}
                assert actual == expected
        db.scalar(select(ProjectMember).where(ProjectMember.project_id == project['id'], ProjectMember.user_id == reader['id'])).active = False
        db.scalar(select(Membership).where(Membership.space_id == org['id'], Membership.user_id == reader['id'])).active = False
        db.commit()
        user = db.get(User, reader['id'])
        assert {ds.id for ds in db.scalars(catalog_query(db, user, app.state.settings))} == {
            ds.id for ds in db.scalars(select(Dataset)) if discover(db, user, ds, app.state.settings)}


def test_catalog_paginates_in_sql_and_query_count_is_bounded(app, client, actors):
    owner = actors['owner']
    with app.state.db.Session() as db:
        for index in range(150):
            ds = Dataset(space_id=owner['space_id'], owner_id=owner['id'], name=f'条目 {index}')
            db.add(ds); db.flush()
            db.add(Version(dataset_id=ds.id, number=1, creator_id=owner['id'], status='published', license='test', source='test'))
        db.commit()
    queries = []
    def collect(_, __, statement, *args):
        queries.append(statement)
    engines = [app.state.db.engine]
    if app.state.async_db is not None:
        engines.append(app.state.async_db.engine.sync_engine)
    for engine in engines:
        event.listen(engine, 'before_cursor_execute', collect)
    try:
        first = client.get('/api/v1/datasets?mine=true&limit=1', headers=owner['headers']).json()
        small = len(queries); queries.clear()
        second = client.get('/api/v1/datasets?mine=true&offset=1&limit=100', headers=owner['headers']).json()
        assert first['total'] == second['total'] == 150
        assert len(second['items']) == 100 and first['items'][0]['id'] not in {r['id'] for r in second['items']}
        assert len(queries) == small and len(queries) <= 12
        # MySQL 使用 LIMIT offset,count；SQLite 使用 LIMIT count OFFSET offset。
        assert any('LIMIT' in query for query in queries)
        assert all(r['published_versions'] == 1 and r['latest_version_id'] for r in second['items'])
    finally:
        for engine in engines:
            event.remove(engine, 'before_cursor_execute', collect)


def test_agent_ranks_before_candidate_limit_and_treats_wildcards_literally(app, client, actors):
    owner = actors['owner']
    with app.state.db.Session() as db:
        # 相关条目先创建，不能被“先截最新100条再排名”遗漏。
        ds = Dataset(space_id=owner['space_id'], owner_id=owner['id'], name='rare_keyword 图像', tags=['100%'])
        db.add(ds); db.flush(); identifier = ds.id
        for index in range(130):
            db.add(Dataset(space_id=owner['space_id'], owner_id=owner['id'], name=f'unrelated-{index}'))
        db.commit()
    from app.assistant_tools import search
    with app.state.db.Session() as db:
        rows = search(db, db.get(User, owner['id']), app.state.settings, 'rare_keyword')
        assert [r['id'] for r in rows] == [identifier]
    response = client.get('/api/v1/datasets?q=%25', headers=owner['headers']).json()
    assert response['total'] == 0  # 门户名称/说明未含百分号，不能把 % 当通配符。
