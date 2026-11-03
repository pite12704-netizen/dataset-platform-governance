import base64
"""生成私有部署密钥与配置。--integration 使用已缓存镜像，仅供本机真实依赖验证。

正式安装必须传入明确镜像引用，尤其 MinIO 必须由运维指定受支持发行版。
不会重置已有密钥，不打印密码，不创建演示账号，也不会删除已有卷。
"""
import argparse
import json
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import Settings


def cached(reference):
    result = subprocess.run(["docker", "image", "inspect", reference], capture_output=True, check=True, text=True)
    row = json.loads(result.stdout)[0]
    if not row.get("RepoDigests"):
        raise RuntimeError("Image has no registry digest: " + reference)
    return row["RepoDigests"][0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--integration", action="store_true")
    for name in ("mysql", "minio", "mc", "redis", "clamav"):
        parser.add_argument("--" + name + "-image")
    args = parser.parse_args()
    refs = {"mysql": "mysql:8.4.11", "minio": "quay.io/minio/minio:latest",
            "mc": "quay.io/minio/mc:latest", "redis": "redis:latest", "clamav": "clamav/clamav:stable"}
    images = {}
    for name, ref in refs.items():
        configured = getattr(args, name + "_image")
        if not configured and not args.integration:
            parser.error("Production requires an explicit --" + name + "-image digest")
        images[name] = configured or cached(ref)
        if "@sha256:" not in images[name]:
            parser.error("Use a registry digest for " + name)
    source = Settings()
    directory = ROOT / "deployment/secrets"
    directory.mkdir(parents=True, exist_ok=True)
    def write(name, value):
        path = directory / name
        if not path.exists():
            path.write_text(value, encoding="utf-8")
        return path.read_text(encoding="utf-8").strip()
    app_key = write("app_key", source.secret_key)
    if app_key != source.secret_key:
        raise RuntimeError("Existing application key differs from source; preserve encrypted MFA data")
    write("session_signing_key", secrets.token_hex(32))
    write("mfa_encryption_keys", json.dumps([base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()]))
    db_password = write("mysql_password", secrets.token_hex(24))
    write("mysql_root_password", secrets.token_hex(32))
    write("mysql_test_password", secrets.token_hex(24))
    write("minio_root_user", "dp_storage_admin")
    write("minio_root_password", secrets.token_hex(32))
    access = write("s3_access_key", "dp_" + secrets.token_hex(10))
    storage_secret = write("s3_secret_key", secrets.token_hex(32))
    test_access = write("s3_test_access_key", "dp_test_" + secrets.token_hex(8))
    test_storage_secret = write("s3_test_secret_key", secrets.token_hex(32))
    redis_password = write("redis_password", secrets.token_hex(24))
    write("database_url", f"mysql+pymysql://dp_app:{db_password}@mysql:3306/dataset_platform?charset=utf8mb4")
    write("redis_url", f"redis://:{redis_password}@redis:6379/0")
    write("backup_key", secrets.token_hex(32))
    service = ROOT / "deployment/service.env.local"
    if not service.exists():
        content = (ROOT / "deployment/service.env.example").read_text(encoding="utf-8")
        service.write_text(content, encoding="utf-8")
    compose = ROOT / "deployment/compose.env.local"
    compose.write_text("\n".join(f"{name.upper()}_IMAGE={image}" for name, image in images.items()) +
                       "\nDP_SERVICE_ENV=./service.env.local\nDP_WEB_BIND=127.0.0.1\nDP_HTTPS_PORT=8443\n", encoding="utf-8")
    host = ROOT / ".env.business.local"
    values = source.model_dump(exclude={name for name in Settings.model_fields if name.endswith("_file")})
    values.update(database_url=f"mysql+pymysql://dp_app:{db_password}@127.0.0.1:13306/dataset_platform?charset=utf8mb4",
                  storage_backend="s3", s3_endpoint="http://127.0.0.1:29000", s3_bucket="datasets",
                  s3_access_key=access, s3_secret_key=storage_secret, redis_url=f"redis://:{redis_password}@127.0.0.1:16379/0",
                  task_mode="celery", scan_mode="clamav", clamav_host="127.0.0.1", clamav_port=13310)
    for name in ('session_signing_key','mfa_encryption_keys'):
        values.pop(name,None)
        values[name+'_file']=str((directory/name).resolve())
    lines = []
    for name, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = str(value).lower()
        elif isinstance(value, (list, dict)):
            value = json.dumps(value)
        lines.append("DP_" + name.upper() + "=" + str(value))
    host.write_text("\n".join(lines) + "\n", encoding="utf-8")
    test = ROOT / ".env.integration.local"
    test_password = (directory / "mysql_test_password").read_text().strip()
    test_lines = [line for line in lines if not line.startswith(("DP_DATABASE_URL=", "DP_S3_BUCKET=", "DP_S3_ACCESS_KEY=", "DP_S3_SECRET_KEY="))]
    test_lines.append(f"DP_DATABASE_URL=mysql+pymysql://dp_test:{test_password}@127.0.0.1:13306/dataset_platform_integration?charset=utf8mb4")
    test_lines.extend(["DP_S3_BUCKET=datasets-integration", "DP_S3_ACCESS_KEY=" + test_access, "DP_S3_SECRET_KEY=" + test_storage_secret])
    test.write_text("\n".join(test_lines) + "\n", encoding="utf-8")
    (ROOT / ".env.recovery.local").write_text("\n".join(test_lines).replace("dataset_platform_integration?", "dataset_platform_recovery?")
                                             .replace("DP_S3_BUCKET=datasets-integration", "DP_S3_BUCKET=datasets-recovery") + "\n", encoding="utf-8")
    print("Private configuration generated; credentials retained in ignored local files.")
    if args.integration:
        print("Storage image is a cached compatibility fixture, not a supported production release.")


if __name__ == "__main__":
    main()
