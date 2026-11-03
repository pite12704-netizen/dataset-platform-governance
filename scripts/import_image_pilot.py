"""本地演练：真实 HTTP 上传、退回重提、授权下载与撤销；不伪造人工属性复核。

只允许 development + 本机 SQLite。账号初始化是唯一直接数据库写入，业务全部走 API。
prepare 可在视觉模型下载期间执行，finish 需要 image_pilot.py analyze 的真实报告。
"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.auth import add_user
from app.config import Settings
from app.db import Database
from app.models import User
from app.schemas import Register
from sqlalchemy import select
from sqlalchemy.engine import make_url

WORK = ROOT / "var/image-pilot"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


class Pilot:
    def __init__(self):
        settings = Settings()
        url = make_url(settings.database_url)
        if settings.environment != "development" or url.get_backend_name() != "sqlite":
            raise RuntimeError("本地演练只能运行在 development + SQLite 环境")
        backup = WORK / "platform-before-pilot.db"
        if not backup.exists():
            with sqlite3.connect(url.database) as source, sqlite3.connect(backup) as target:
                source.backup(target)
        path = WORK / "test-accounts.json"
        if not path.exists():
            save(path, {role: {"email": f"image-pilot-{role}@example.test", "password": secrets.token_urlsafe(24),
                               "name": f"图像试验-{role}"} for role in ["owner", "reviewer", "publisher", "reader", "outsider"]})
        self.accounts = read(path)
        if 'publisher' not in self.accounts:
            self.accounts['publisher']={'email':'image-pilot-publisher@example.test','password':secrets.token_urlsafe(24),'name':'图像试验-发布管理员'}
            save(path,self.accounts)
        database = Database(settings.database_url)
        with database.Session() as db:
            for data in self.accounts.values():
                if not db.scalar(select(User).where(User.email == data["email"])):
                    add_user(db, Register(**data), settings, verified=True)
            db.commit()
        self.path = WORK / "platform-state.json"
        self.state = read(self.path) if self.path.exists() else {"workflow": [], "assets": {}}
        self.clients = {}
        for role, account in self.accounts.items():
            self.clients[role] = httpx.Client(base_url="http://127.0.0.1:8000/api/v1", trust_env=False,
                                              headers={"X-Requested-With": "dataset-platform"}, timeout=120)
            token = self.call(role, "POST", "/auth/login", json={k: account[k] for k in ["email", "password"]}).json()["access_token"]
            self.clients[role].headers["Authorization"] = "Bearer " + token

    def persist(self):
        save(self.path, self.state)

    def call(self, role, method, path, expected=None, **kwargs):
        for _ in range(4):
            response = self.clients[role].request(method, path, **kwargs)
            if response.status_code != 429:
                break
            delay = min(60, max(1, int(response.headers.get("Retry-After", "60"))))
            print(f"API 限流，等待 {delay} 秒后续传", flush=True)
            time.sleep(delay)
        if expected is not None:
            if response.status_code != expected:
                raise RuntimeError(f"{method} {path}: expected {expected}, got {response.status_code}: {response.text[:500]}")
        else:
            response.raise_for_status()
        return response

    def checkpoint(self, name, detail):
        if not any(x["name"] == name for x in self.state["workflow"]):
            self.state["workflow"].append({"name": name, "passed": True, "detail": detail})
        self.persist()
        print("通过：" + name, flush=True)

    def upload(self, version, path, filename=None):
        filename = filename or path.name
        data = path.read_bytes()
        key = version + ":" + filename
        if key in self.state["assets"]:
            return self.state["assets"][key]
        digest = hashlib.sha256(data).hexdigest()
        session = self.call("owner", "POST", f"/versions/{version}/uploads", json={"filename": filename,
            "size": len(data), "sha256": digest, "preview_allowed": False,
            "idempotency_key": hashlib.sha256((key + digest).encode()).hexdigest()}).json()
        for number, offset in enumerate(range(0, len(data), session["part_size"]), 1):
            self.call("owner", "PUT", f"/uploads/{session['id']}/parts/{number}", content=data[offset:offset+session["part_size"]])
        result = self.call("owner", "POST", f"/uploads/{session['id']}/complete").json()
        again = self.call("owner", "POST", f"/uploads/{session['id']}/complete").json()
        assert result["asset_id"] == again["asset_id"], "重复完成上传生成了新资产"
        self.state["assets"][key] = result["asset_id"]
        self.persist()
        return result["asset_id"]

    def metadata(self, curated=False):
        return {"license": "CC-BY-2.0", "source": "COCO 2017 validation；原图许可依据 COCO 元数据；逐图来源保存在本地导出清单。",
            "card": {"status": "experimental_pending_human_review", "human_reviewed": 0,
                     "description": "机器候选标签，未完成真人复核；不得用作已验证属性真值。",
                     "split": "COCO validation 定向抽样，仅作流程试验；未划分训练集，不用于报告模型准确率。",
                     "task": "图片去重、人物衣着属性候选生成与多标签筛选；待人工复核。",
                     "schema": "人物框 bbox=[x,y,width,height] 来自 COCO；hat、sleeve、upper_color 为模型候选，review_status=pending_human_review。",
                     "person_boxes": "COCO ground truth", "controlled_duplicate_fixtures": 0 if curated else 8},
            "changelog": "去重与 LM Studio 视觉模型候选标签" if curated else "原始图片与受控重复副本，保留原始数据"}

    def new_version(self, label):
        if label not in self.state:
            self.state[label] = self.call("owner", "POST", f"/datasets/{self.state['dataset']}/versions",
                                          json=self.metadata(label == "curated")).json()["id"]
            self.persist()
        return self.state[label]

    def publish(self, version, label):
        current = self.call("owner", "GET", f"/versions/{version}").json()
        assert all(a["status"] == "clean" for a in current["assets"]), "存在未通过检查的文件"
        if current["status"] == "published":
            return
        pending = label + "_publication"
        if pending not in self.state:
            self.state[pending] = self.call("owner", "POST", f"/versions/{version}/submit").json()["id"]
            self.persist()
        self.call("publisher", "POST", f"/publications/{self.state[pending]}/decision", json={"approve": True,
            "comment": "自动化流程演练：仅发布到测试组织。人物属性仍待真实人工复核。"})

    def prepare(self):
        if "space" not in self.state:
            self.state["space"] = self.call("owner", "POST", "/spaces", json={"name": "COCO 图像试验组织"}).json()["id"]
            self.persist()
        for role in ["reviewer", "publisher", "reader"]:
            self.call("owner", "POST", f"/spaces/{self.state['space']}/members", json={
                "email": self.accounts[role]["email"], "role": {'reviewer':'reviewer','publisher':'admin','reader':'member'}[role]})
        if "dataset" not in self.state:
            self.state["dataset"] = self.call("owner", "POST", "/datasets", json={"space_id": self.state["space"],
                "name": "COCO 人物图像试验（机器标签待复核）", "description": "24 张原图：单人、多人人物；去重与衣着属性识别的本地小样本试验。",
                "modality": "image", "category": "人物图像", "tags": ["COCO", "LM Studio", "待人工复核"],
                "visibility": "organization", "access_mode": "application"}).json()["id"]
            self.persist()
        version = self.new_version("raw")
        for item in read(WORK / "dedup_test.json")["inputs"]:
            self.upload(version, WORK / item["relative_path"])
        self.checkpoint("分片上传与重复提交", "32 个原始文件/测试副本；重复完成请求返回同一资产 ID")
        if not self.state.get("rejection_done"):
            if "rejected_publication" not in self.state:
                self.state["rejected_publication"] = self.call("owner", "POST", f"/versions/{version}/submit").json()["id"]
                self.persist()
            path = f"/publications/{self.state['rejected_publication']}/decision"
            self.call("owner", "POST", path, expected=403, json={"approve": True})
            self.call("publisher", "POST", path, json={"approve": False, "comment": "演练退回：补充来源与机器标签未复核说明"})
            self.call("owner", "PUT", f"/versions/{version}", json=self.metadata())
            self.state["rejection_done"] = True
            self.persist()
        self.publish(version, "raw")
        self.checkpoint("发布退回与重提", "提交者自审被拒绝；另一测试审核账号退回后重提通过")
        self.call("outsider", "GET", f"/datasets/{self.state['dataset']}", expected=404)
        first = read(WORK / "source_manifest.json")["images"][0]
        asset = self.state["assets"][version + ":" + first["filename"]]
        path = f"/assets/{asset}/download"
        self.call("outsider", "GET", path, expected=403)
        self.checkpoint("组织隔离", "组织外账号看不到目录，无法下载受限文件")
        if not self.state.get("grant_test_done"):
            payload = {"version_id": version, "purpose": "本地图片清洗流程自动化演练", "duration_days": 7,
                       "idempotency_key": "image-pilot-raw-application"}
            if "application" not in self.state:
                self.call("reader", "GET", path, expected=403)
                self.state["application"] = self.call("reader", "POST", "/applications", json=payload).json()["id"]
                self.persist()
            if not self.state.get("application_approved"):
                self.call("owner", "POST", f"/applications/{self.state['application']}/decision", json={"approve": True})
                self.call("reader", "GET", path, expected=403)
                self.state["application_approved"] = True
                self.persist()
            self.call("reader", "POST", f"/versions/{version}/accept-license")
            original = (WORK / "originals" / first["filename"]).read_bytes()
            assert self.call("reader", "GET", path, expected=206, headers={"Range": "bytes=0-7"}).content == original[:8]
            assert hashlib.sha256(self.call("reader", "GET", path).content).hexdigest() == first["sha256"]
            assert self.call("reader", "POST", "/applications", json=payload).json()["id"] == self.state["application"]
            grants = self.call("owner", "GET", "/grants").json()
            matched = [g for g in grants if g["version_id"] == version]
            assert len(matched) == 1
            self.call("owner", "POST", f"/grants/{matched[0]['id']}/revoke", json={"reason": "本地测试完成，验证即时撤销"})
            self.call("reader", "GET", path, expected=403)
            self.state["grant_test_done"] = True
            self.persist()
        self.checkpoint("申请、许可、下载与撤销", "审批后仍需确认许可；断点下载和 SHA-256 通过；重复申请不重复授权；撤销后新下载被阻断")
        job = self.call("owner", "POST", "/jobs", json={"version_id": version, "kind": "quality",
                        "idempotency_key": "image-pilot-raw-quality"}).json()
        assert job["status"] == "completed", job
        self.checkpoint("基础质量任务", "32 文件规则检查任务实际完成；开发扫描不等于生产恶意文件检测")
        print("原始版本流程已完成", flush=True)

    def finish(self):
        if not self.state.get("grant_test_done"):
            self.prepare()
        report = read(WORK / "report.json")
        assert report["model"]["runtime"].startswith("LM Studio"), "本轮选择 LM Studio，不能静默换用其他模型"
        version = self.new_version("curated")
        for image in report["images"]:
            self.upload(version, WORK / "originals" / image["filename"])
        display = {"schema_version": 1, "model_name": "LM Studio · Qwen3-VL-4B", "summary": report["summary"],
                   "fixture_count": 8, "workflow": self.state["workflow"], "images": [], "duplicates": []}
        for row in report["images"]:
            # 网络展示只包含必要字段；详细来源与哈希保留在本地带来源的导出清单。
            display["images"].append({**{k: row[k] for k in ["id", "filename", "width", "height", "person_count", "persons"]},
                                      "asset_id": self.state["assets"][version + ":" + row["filename"]]})
        for duplicate in report["duplicates"]:
            display["duplicates"].append({**duplicate,
                "asset_id": self.state["assets"][self.state["raw"] + ":" + duplicate["filename"]],
                "keep_asset_id": self.state["assets"][version + ":" + duplicate["keep"]]})
        save(WORK / "image_pilot_report.json", display)
        self.upload(version, WORK / "image_pilot_report.json")
        self.publish(version, "curated")
        self.call("reader", "GET", f"/assets/{display['images'][0]['asset_id']}/download", expected=403)
        self.checkpoint("版本授权隔离", "旧版本申请不覆盖新的去重版本")
        self.state["page_url"] = "http://127.0.0.1:5173/image-pilot/" + version
        self.persist()
        print("试验结果页面：" + self.state["page_url"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "finish"])
    args = parser.parse_args()
    pilot = Pilot()
    getattr(pilot, args.stage)()
