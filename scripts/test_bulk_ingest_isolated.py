"""新建一次性 MySQL/MinIO 容器验证导入；不连接或清理任何现有项目服务。"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

import httpx
import pymysql
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, check=True):
    result = subprocess.run(['docker', *args], cwd=ROOT, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError('Isolated Docker action failed: ' + args[0])
    return result.stdout.strip()


def main():
    run = uuid.uuid4().hex[:12]
    directory = ROOT / 'var' / 'test-runs' / ('bulk-' + run)
    directory.mkdir(parents=True)
    names = ['dp-ingest-check-' + run + '-mysql', 'dp-ingest-check-' + run + '-minio']
    image_values = {}
    for line in (ROOT/'deployment/compose.env.local').read_text().splitlines():
        name, _, value = line.partition('=')
        if name in {'MYSQL_IMAGE', 'MINIO_IMAGE'}:
            image_values[name] = value
    for image in image_values.values():
        docker('image', 'inspect', image, '--format', '{{.Id}}')
    password, root_password, access, secret = secrets.token_hex(24), secrets.token_hex(24), 'ingest' + run, secrets.token_hex(24)
    (directory/'mysql.env').write_text(f'MYSQL_ROOT_PASSWORD={root_password}\nMYSQL_DATABASE=dataset_platform_ingest_test\nMYSQL_USER=dp_ingest_test\nMYSQL_PASSWORD={password}\n')
    (directory/'minio.env').write_text(f'MINIO_ROOT_USER={access}\nMINIO_ROOT_PASSWORD={secret}\n')
    created = []
    try:
        # 名称由本次随机 run 生成；只记录并回收本次创建的容器。无宿主目录或业务卷挂载。
        created.append(names[0])
        docker('run', '-d', '--pull', 'never', '--name', names[0], '--memory', '1g',
            '--env-file', str(directory/'mysql.env'), '-p', '127.0.0.1::3306',
            '--tmpfs', '/var/lib/mysql:rw,size=512m', image_values['MYSQL_IMAGE'],
            '--innodb-buffer-pool-size=64M', '--innodb-redo-log-capacity=64M', '--max-connections=30')
        created.append(names[1])
        docker('run', '-d', '--pull', 'never', '--name', names[1], '--memory', '512m',
            '--env-file', str(directory/'minio.env'), '-p', '127.0.0.1::9000',
            '--tmpfs', '/data:rw,size=128m', image_values['MINIO_IMAGE'], 'server', '/data')
        mysql_port = int(json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Ports}}', names[0]))['3306/tcp'][0]['HostPort'])
        minio_port = int(json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Ports}}', names[1]))['9000/tcp'][0]['HostPort'])
        deadline = time.monotonic() + 120
        print('Waiting for newly created isolated MySQL and MinIO...', flush=True)
        while True:
            try:
                connection = pymysql.connect(host='127.0.0.1', port=mysql_port, user='dp_ingest_test', password=password,
                    database='dataset_platform_ingest_test', connect_timeout=2)
                connection.close()
                if httpx.get(f'http://127.0.0.1:{minio_port}/minio/health/ready', timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('New isolated services did not become ready')
            time.sleep(1)
        env = {**{key:value for key,value in os.environ.items() if not key.startswith('DP_')}, 'DP_INGEST_ISOLATED_RUN': run,
            'DP_INGEST_TEST_DATABASE_URL': URL.create('mysql+pymysql', username='dp_ingest_test', password=password,
                host='127.0.0.1', port=mysql_port, database='dataset_platform_ingest_test').render_as_string(hide_password=False),
            'DP_INGEST_TEST_S3_ENDPOINT': f'http://127.0.0.1:{minio_port}',
            'DP_INGEST_TEST_S3_ACCESS': access, 'DP_INGEST_TEST_S3_SECRET': secret}
        return subprocess.run([sys.executable, '-m', 'pytest', 'backend/tests/test_ingest_mysql.py', '-q',
            '--basetemp', str(directory/'pytest')], cwd=ROOT, env=env).returncode
    finally:
        for name in reversed(created):
            docker('rm', '-f', name, check=False)
        # 只删除本次生成的测试凭据文件；保留非敏感测试结果以便排查。
        for name in ('mysql.env', 'minio.env'):
            (directory/name).unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
