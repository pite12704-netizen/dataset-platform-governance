"""小规模线上链路压测：真实 HTTP/MySQL/Redis，专用测试账号与合成图片。

只能使用 dp_test 的独立 review_concurrency 库。不会发布、审批或写入业务库。
此测试不验证模型准确率、GB 文件吞吐、长期稳定性或生产容量。
"""
import argparse
import hashlib
import io
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def settings_for(run):
    from app.config import Settings
    from sqlalchemy.engine import make_url
    source = Settings(_env_file=ROOT / '.env.integration.local', environment='test')
    url = make_url(source.database_url)
    if url.username != 'dp_test' or url.database != 'dataset_platform_integration':
        raise RuntimeError('Refusing non-test database configuration')
    return Settings(_env_file=None, environment='test', deployment='private',
        secret_key='isolated-pilot-load-key-01234567890123456789',
        database_url=url.set(database='dataset_platform_review_concurrency').render_as_string(hide_password=False),
        storage_backend='local', storage_root=ROOT / 'var/run' / run['id'] / 'objects',
        mail_spool=ROOT / 'var/run' / run['id'] / 'mail', task_mode='manual', scan_mode='development',
        require_admin_mfa=False, require_verification=False, redis_url=source.redis_url,
        public_url=run['api_url'], frontend_url='http://127.0.0.2:15174',
        cors_origins=['http://127.0.0.2:15174'], rate_limit_per_minute=120, login_limit_per_minute=10000)


def create_load_app():
    from app.main import create_app
    from redis.asyncio import Redis
    run = json.loads(Path(os.environ['DP_PILOT_RUN_FILE']).read_text(encoding='utf-8'))
    app = create_app(settings_for(run))
    app.state.rate_limiter.redis = Redis.from_url(app.state.settings.redis_url,
        socket_connect_timeout=2, socket_timeout=2)
    return app


def seed(run, count):
    from fastapi.testclient import TestClient
    from PIL import Image
    from app.auth import add_user
    from app.image_reviews import create_batch, mark_submitted
    from app.main import create_app
    from app.models import Asset, Dataset, Membership, Space, User, Version
    from app.schemas import Register
    app = create_app(settings_for(run)); actors = []
    password = 'Isolated-pilot-test-password!'
    with app.state.db.Session() as db:
        for index in range(count + 1):
            user = add_user(db, Register(email=f'pilot.{run["id"]}.{index}@example.test',
                name=f'压测测试账号 {index}', password=password), app.state.settings, verified=True, admin=False)
            db.flush(); actors.append({'id': user.id, 'email': user.email})
        owner = db.get(User, actors[0]['id'])
        space = Space(name='隔离多人压测 ' + run['id'], owner_id=owner.id)
        db.add(space); db.flush()
        for index, actor in enumerate(actors):
            db.add(Membership(space_id=space.id, user_id=actor['id'], role='admin' if index == 0 else 'reviewer'))
        for index in range(150):
            dataset = Dataset(space_id=space.id, owner_id=owner.id, name=f'压测目录 {index}', visibility='organization')
            db.add(dataset); db.flush()
            db.add(Version(dataset_id=dataset.id, number=1, creator_id=owner.id, status='published', license='test-only', source='synthetic metadata'))
        dataset = Dataset(space_id=space.id, owner_id=owner.id, name='隔离合成候选', visibility='organization')
        db.add(dataset); db.flush()
        version = Version(dataset_id=dataset.id, number=1, creator_id=owner.id, license='test-only', source='synthetic pixels')
        db.add(version); db.flush()
        image = io.BytesIO(); Image.new('RGB', (64, 80), (40, 100, 150)).save(image, 'PNG')
        key = f'pilot/{run["id"]}/synthetic-image'; raw = image.getvalue(); app.state.storage.put_bytes(key, raw)
        asset = Asset(version_id=version.id, filename='synthetic.png', object_key=key, size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(), status='clean')
        db.add(asset); db.flush(); space.used_bytes = len(raw)
        rows = [{'asset_id': asset.id, 'person_key': str(index), 'width': 64, 'height': 80,
            'annotation': {'bbox': [4, 5, 30, 60], 'labels': {'hat': 'hat', 'sleeve': 'short_sleeves', 'upper_color': 'red'}}}
            for index in range(24)]
        batch = create_batch(db, version.id, owner.id, rows, {'origin': 'isolated-load-test', 'model': 'synthetic', 'images': 1})
        mark_submitted(db, owner, batch)
        duplicate = create_batch(db, version.id, owner.id, rows[:1], {'origin': 'isolated-load-test', 'model': 'alternate synthetic', 'images': 1})
        db.commit()
        run.update(batch_id=batch.id, duplicate_id=duplicate.id, actors=actors, password=password)
    with TestClient(app) as client:
        for actor in actors:
            reply = client.post('/api/v1/auth/login', json={'email': actor['email'], 'password': password})
            assert reply.status_code == 200
            actor['token'] = reply.json()['access_token']; client.cookies.clear()
    return actors


def main():
    import httpx
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requests', type=int, default=400)
    parser.add_argument('--concurrency', type=int, default=16)
    parser.add_argument('--users', type=int, default=8)
    parser.add_argument('--port', type=int, default=18002)
    parser.add_argument('--report', default='var/operations/pilot-load-results.json')
    parser.add_argument('--keep-server', action='store_true', help='Retain isolated API for subsequent browser QA')
    args = parser.parse_args()
    if not (1 <= args.requests <= 20000 and 1 <= args.concurrency <= 64 and 2 <= args.users <= 24 and 1024 <= args.port <= 65535):
        parser.error('Parameters exceed pilot limits')
    run = {'id': 'pilot-' + uuid.uuid4().hex, 'api_url': f'http://127.0.0.2:{args.port}'}
    run_file = ROOT / 'var/run' / (run['id'] + '.json'); run_file.parent.mkdir(parents=True, exist_ok=True)
    actors = seed(run, args.users)
    # 文件只有隔离测试凭据，不能包含真实数据库 URL 或存储密钥。
    run_file.write_text(json.dumps(run, ensure_ascii=False), encoding='utf-8')
    environment = dict(os.environ, DP_PILOT_RUN_FILE=str(run_file))
    log = (ROOT / 'var/run' / (run['id'] + '.log')).open('w', encoding='utf-8')
    command = [sys.executable, '-m', 'uvicorn', '--app-dir', 'scripts', 'pilot_load:create_load_app', '--factory',
        '--host', '127.0.0.2', '--port', str(args.port), '--workers', '2', '--no-access-log', '--limit-concurrency', '64']
    process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log, stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    local = threading.local(); clients = []; client_lock = threading.Lock()
    try:
        with httpx.Client(trust_env=False, timeout=5) as probe:
            for attempt in range(60):
                try:
                    if probe.get(run['api_url'] + '/health/ready').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError('Isolated API failed; inspect its var/run log')
                time.sleep(0.5)
            else:
                raise RuntimeError('Isolated API did not become ready')
        def request(index):
            if not hasattr(local, 'client'):
                local.client = httpx.Client(trust_env=False, timeout=20)
                with client_lock:
                    clients.append(local.client)
            actor = actors[1 + index % args.users]; started = time.perf_counter()
            reply = local.client.get(run['api_url'] + '/api/v1/datasets?limit=24', headers={'Authorization': 'Bearer ' + actor['token']})
            elapsed = (time.perf_counter() - started) * 1000
            if reply.status_code == 200:
                assert reply.json()['total'] == 150 and len(reply.json()['items']) == 24
            return reply.status_code, elapsed, reply.headers.get('x-request-id')
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(request, range(args.requests)))
        duration = time.perf_counter() - started; latencies = sorted(r[1] for r in results)
        with httpx.Client(base_url=run['api_url'], trust_env=False, timeout=20) as client:
            def claim(actor):
                return client.post(f"/api/v1/image-reviews/{run['batch_id']}/claim-next", json={},
                    headers={'Authorization': 'Bearer ' + actor['token']})
            with ThreadPoolExecutor(max_workers=args.users) as pool:
                claimed = list(pool.map(claim, actors[1:]))
            assert all(reply.status_code == 200 and reply.json()['item'] for reply in claimed)
            assert len({reply.json()['item']['id'] for reply in claimed}) == args.users
            for actor, reply in zip(actors[1:], claimed):
                item = reply.json()
                assert client.post(f"/api/v1/image-reviews/items/{item['item']['id']}/release", json={'claim_token': item['claim_token']},
                    headers={'Authorization': 'Bearer ' + actor['token']}).status_code == 200
        report = {'database': 'dataset_platform_review_concurrency', 'synthetic_only': True,
            'api_workers': 2, 'real_dependencies': ['HTTP', 'MySQL', 'Redis'], 'catalog_datasets': 150,
            'users_shared_ip': args.users, 'concurrency': args.concurrency, 'requests': args.requests,
            'status_counts': {str(code): sum(r[0] == code for r in results) for code in {r[0] for r in results}},
            'elapsed_seconds': round(duration, 3), 'requests_per_second': round(args.requests / duration, 2),
            'latency_ms': {'p50': round(statistics.median(latencies), 2), 'p95': round(latencies[math.ceil(len(latencies)*0.95)-1], 2), 'max': round(max(latencies), 2)},
            'unique_request_ids': len({r[2] for r in results if r[2]}), 'distinct_parallel_claims': args.users,
            'limitations': ['本机短时目录读与任务领取，不代表生产容量', '未测试GB文件、模型识别、持续负载或跨主机故障'],
            'run_file': str(run_file), 'api_pid': process.pid if args.keep_server else None}
        target = ROOT / args.report; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: value for key, value in report.items() if key not in {'run_file', 'api_pid'}}, ensure_ascii=False))
        assert all(r[0] == 200 for r in results), 'Pilot HTTP failures observed; inspect report'
    finally:
        for client in clients:
            client.close()
        if not args.keep_server:
            if os.name == 'nt':
                # Uvicorn 多进程包含子进程，单独 terminate 父进程会留下测试监听端口。
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                process.terminate()
            process.wait(timeout=15)
        log.close()


if __name__ == '__main__':
    main()
