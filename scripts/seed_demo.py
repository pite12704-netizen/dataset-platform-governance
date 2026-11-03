"""Create clearly labelled synthetic fixtures, exclusively in a local development database."""
import hashlib
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import add_user
from app.config import get_settings
from app.main import create_app
from app.models import User
from app.schemas import Register


def main():
    settings = get_settings()
    if settings.environment != "development" or not settings.database_url.startswith("sqlite"):
        raise RuntimeError("演示数据仅允许写入 development SQLite 数据库")
    settings = settings.model_copy(update={"require_admin_mfa": False, "task_mode": "inline"})
    app = create_app(settings)
    credentials = {}
    with app.state.db.Session() as db:
        if db.scalar(select(User).where(User.email == "contributor@example.test")):
            print("Local demo already initialized; no existing data changed.")
            return
        for email, name, admin in [("contributor@example.test", "数据贡献者", False),
                                   ("reviewer@example.test", "平台审核员", True)]:
            password = secrets.token_urlsafe(20)
            add_user(db, Register(email=email, name=name, password=password), settings, verified=True, admin=admin)
            credentials[email] = password
        db.commit()
    bootstrap = ROOT / "var" / "development-accounts.json"
    bootstrap.write_text(json.dumps(credentials, ensure_ascii=False, indent=2), encoding="utf-8")
    with TestClient(app) as client:
        headers = {}
        for email, password in credentials.items():
            result = client.post("/api/v1/auth/login", json={"email": email, "password": password})
            result.raise_for_status()
            headers[email] = {"Authorization": "Bearer " + result.json()["access_token"]}
            client.cookies.clear()
        owner, reviewer = headers["contributor@example.test"], headers["reviewer@example.test"]
        space = client.get("/api/v1/spaces", headers=owner).json()[0]
        fixtures = [
            ("中文问答入门集 · 示例", "三条自编问答样本，用于验证 JSONL 导入、版本发布与申请流程。", "text", ["问答", "指令微调", "合成样本"], "application",
             'question,answer\n什么是数据集,用于分析与训练的数据集合\n什么是版本,一份不可变的数据快照\n什么是授权,指定主体在期限内的访问许可\n'),
            ("客服意图分类集 · 示例", "覆盖咨询、售后与物流场景的小型合成语料，适合体验目录检索和开放获取。", "text", ["意图识别", "客服", "合成样本"], "open",
             'text,label\n如何选择商品,咨询\n申请维修服务,售后\n查看发货进度,物流\n'),
            ("商品属性评测集 · 示例", "三条虚构商品属性记录，展示表格数据管理、质量报告及格式转换。", "tabular", ["结构化", "评测", "合成样本"], "open",
             'item,category,color\n样品一,文具,蓝色\n样品二,收纳,白色\n样品三,纸品,绿色\n'),
        ]
        for name, description, modality, tags, access, content in fixtures:
            ds = client.post("/api/v1/datasets", headers=owner, json={"space_id":space["id"],"name":name,
                  "description":description,"modality":modality,"tags":tags,"visibility":"public","access_mode":access}).json()
            v = client.post(f"/api/v1/datasets/{ds['id']}/versions", headers=owner, json={"license":"CC0-1.0（仅自编示例内容）",
                 "source":"本项目自编合成样本；不包含真实人员或业务记录", "card":{"task":"流程验收", "split":"仅演示集", "schema":"CSV 首行为字段名称"}, "changelog":"首个演示版本"}).json()
            raw = content.encode("utf-8")
            upload = client.post(f"/api/v1/versions/{v['id']}/uploads", headers=owner, json={"filename":"samples.csv",
                "size":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"idempotency_key":secrets.token_hex(16),"preview_allowed":True}).json()
            client.put(f"/api/v1/uploads/{upload['id']}/parts/1", headers=owner, content=raw).raise_for_status()
            client.post(f"/api/v1/uploads/{upload['id']}/complete",headers=owner).raise_for_status()
            p = client.post(f"/api/v1/versions/{v['id']}/submit",headers=owner)
            p.raise_for_status()
            client.post(f"/api/v1/publications/{p.json()['id']}/decision",headers=reviewer,json={"approve":True,"comment":"已核验自编合成样本"}).raise_for_status()
            client.post("/api/v1/jobs",headers=owner,json={"version_id":v["id"],"kind":"quality","idempotency_key":secrets.token_hex(16)}).raise_for_status()
    print("Created 3 labelled synthetic datasets. Local credentials saved under var/development-accounts.json.")


if __name__ == "__main__":
    main()
