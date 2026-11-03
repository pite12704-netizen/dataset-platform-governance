"""Docker Desktop 无法挂载 E 盘时，将少量挂载配置放入本机临时目录。

业务数据仍在 Docker 持久卷；源配置仍在项目中。仅复制密钥与存储初始化文件，
不读取或改动其他项目。正式 Linux 部署直接使用 deployment/compose.yml。
"""
import json
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
record = ROOT / "var/run/desktop-stage.json"
record.parent.mkdir(parents=True, exist_ok=True)
if record.exists():
    stage = Path(json.loads(record.read_text())["path"]).resolve()
    if not stage.is_relative_to(Path(tempfile.gettempdir()).resolve()) or not stage.name.startswith("dataset-platform-"):
        raise RuntimeError("Invalid staging directory")
else:
    stage = Path(tempfile.mkdtemp(prefix="dataset-platform-"))
    record.write_text(json.dumps({"path": str(stage)}), encoding="utf-8")
secrets_dir = stage / "secrets"
secrets_dir.mkdir(parents=True, exist_ok=True)
definitions = {}
for source in (ROOT / "deployment/secrets").iterdir():
    if source.is_file():
        shutil.copy2(source, secrets_dir / source.name)
        definitions[source.name] = {"file": (secrets_dir / source.name).as_posix()}
volumes = []
for name in ("storage-init.sh", "bucket-policy.json", "integration-init.sh", "integration-policy.json"):
    shutil.copy2(ROOT / "deployment" / name, stage / name)
    volumes.append(f"{(stage / name).as_posix()}:/bootstrap/{name}:ro")
overlay = {"secrets": definitions, "services": {"storage-init": {"volumes": volumes,
           "secrets": ["s3_test_access_key", "s3_test_secret_key"]}}}
(ROOT / "deployment/compose.desktop.local").write_text(json.dumps(overlay), encoding="utf-8")
print("Docker Desktop mount compatibility overlay prepared.")
