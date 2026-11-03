"""在本项目 MySQL/MinIO 中创建隔离验证库和桶；不接触已有业务记录。"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
compose = ["docker", "compose", "--env-file", "deployment/compose.env.local", "-f", "deployment/compose.yml",
           "-f", "deployment/compose.local.yml", "-f", "deployment/compose.desktop.local"]
password = (ROOT / "deployment/secrets/mysql_test_password").read_text().strip()
if not password.isalnum():
    raise RuntimeError("Unexpected test password format")
sql = f"""CREATE DATABASE IF NOT EXISTS dataset_platform_integration CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS dataset_platform_recovery CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'dp_test'@'%' IDENTIFIED BY '{password}';
GRANT ALL ON dataset_platform_integration.* TO 'dp_test'@'%';
GRANT ALL ON dataset_platform_recovery.* TO 'dp_test'@'%';
"""
subprocess.run(compose + ["exec", "-T", "mysql", "sh", "-c",
               'MYSQL_PWD="$(cat /run/secrets/mysql_root_password)" exec mysql -u root'],
               input=sql, text=True, check=True, cwd=ROOT)
subprocess.run(compose + ["run", "--rm", "--no-deps", "--entrypoint", "/bin/sh", "storage-init",
                         "/bootstrap/integration-init.sh"], check=True, cwd=ROOT)
print("Isolated integration resources created; no business database grants changed.")
