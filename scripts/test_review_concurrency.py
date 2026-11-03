"""创建固定隔离库并执行真实 MySQL 审核并发测试，不清空数据库、不连接业务库。"""
import os
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--report', type=Path, default=ROOT / 'var/operations/mysql-concurrency-results.xml')
args = parser.parse_args()
sys.path.insert(0, str(ROOT / 'backend'))
from app.config import Settings
from sqlalchemy.engine import make_url

cfg = Settings(_env_file=ROOT / '.env.integration.local', environment='test')
url = make_url(cfg.database_url)
if url.username != 'dp_test' or url.database != 'dataset_platform_integration':
    raise RuntimeError('Expected dedicated dp_test integration configuration; refusing business database access')
database = 'dataset_platform_review_concurrency'
password = (ROOT / 'deployment/secrets/mysql_test_password').read_text().strip()
if not password.isalnum():
    raise RuntimeError('Unexpected test password format')
compose = ['docker', 'compose', '--env-file', 'deployment/compose.env.local', '-f', 'deployment/compose.yml',
           '-f', 'deployment/compose.local.yml', '-f', 'deployment/compose.desktop.local']
sql = f"""CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'dp_test'@'%' IDENTIFIED BY '{password}';
GRANT ALL ON `{database}`.* TO 'dp_test'@'%';
"""
subprocess.run(compose + ['exec', '-T', 'mysql', 'sh', '-c',
    'MYSQL_PWD="$(cat /run/secrets/mysql_root_password)" exec mysql -u root'],
    input=sql, text=True, check=True, cwd=ROOT)
test_url = url.set(database=database).render_as_string(hide_password=False)
environment = dict(os.environ, DP_DATABASE_URL=test_url, DP_ENVIRONMENT='test', DP_REVIEW_TEST_URL=test_url)
subprocess.run([sys.executable, '-m', 'alembic', '-c', 'backend/alembic.ini', 'upgrade', 'head'],
               env=environment, cwd=ROOT, check=True)
test_temp = ROOT / 'var' / 'run' / ('mysql-tests-' + __import__('uuid').uuid4().hex)
subprocess.run([sys.executable, '-m', 'pytest', 'backend/tests/test_review_mysql.py',
               'backend/tests/test_publication_mysql.py', 'backend/tests/test_catalog_mysql.py',
               'backend/tests/test_upload_tasks_mysql.py', 'backend/tests/test_agent_mysql.py', 'backend/tests/test_governance_mysql.py', '-q', '--tb=short', '--show-capture=no', '--basetemp', str(test_temp),
               '--junitxml', str(args.report)],
               env=environment, cwd=ROOT, check=True)
print('PASS: MySQL concurrency and catalog isolation; only the dedicated review test database was used.')
