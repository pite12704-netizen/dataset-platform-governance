"""核验实际产物、平台预览和 Agent 模型调用；失败直接抛错，不编造完成状态。"""
import base64
import hashlib
import io
import json
from pathlib import Path
import statistics
import time
import zipfile

import httpx
from PIL import Image

WORK = Path(__file__).resolve().parents[1] / "var/image-pilot"


def read(name):
    return json.loads((WORK / name).read_text(encoding="utf-8"))


class RetryClient(httpx.Client):
    def request(self, *args, **kwargs):
        for _ in range(4):
            response = super().request(*args, **kwargs)
            if response.status_code != 429:
                response.raise_for_status()
                return response
            delay = min(60, max(1, int(response.headers.get("Retry-After", "60"))))
            print(f"Respecting API rate limit; retry in {delay}s", flush=True)
            time.sleep(delay)
        response.raise_for_status()


def main():
    report, state, accounts = read("report.json"), read("platform-state.json"), read("test-accounts.json")
    model = httpx.get("http://127.0.0.1:1234/v1/models", trust_env=False).json()
    assert any(m["id"] == "image-pilot-qwen3-vl-4b" for m in model["data"])
    client = RetryClient(base_url="http://127.0.0.1:8000/api/v1", trust_env=False, timeout=120,
                          headers={"X-Requested-With": "dataset-platform"})
    response = client.post("/auth/login", json={k: accounts["owner"][k] for k in ["email", "password"]})
    response.raise_for_status()
    version = client.get("/versions/" + state["curated"]).json()
    assert version["status"] == "published" and version["can_download"]
    assert all(a["status"] == "clean" for a in version["assets"])
    display = read("image_pilot_report.json")
    for row in display["images"]:
        response = client.get("/assets/" + row["asset_id"] + "/preview")
        response.raise_for_status()
        preview = response.json()
        assert preview["kind"] == "image" and preview["content"].startswith("data:image/jpeg;base64,")
        with Image.open(io.BytesIO(base64.b64decode(preview["content"].split(",", 1)[1], validate=True))) as image:
            image.verify()
    originals = {row["filename"]: row for row in read("source_manifest.json")["images"]}
    for row in originals.values():
        assert hashlib.sha256((WORK / "originals" / row["filename"]).read_bytes()).hexdigest() == row["sha256"]
    exports = {}
    for path in (WORK / "exports").glob("*.zip"):
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["status"] == "experimental_pending_human_review"
            for row in manifest["images"]:
                assert hashlib.sha256(archive.read("images/" + row["filename"])).hexdigest() == originals[row["filename"]]["sha256"]
            exports[path.stem] = len(manifest["images"])
    response = client.post("/agent/chat", json={"message": "COCO", "intent": "search"})
    response.raise_for_status()
    agent = response.json()
    assert agent["status"] == "completed", agent.get("failure_reason")
    assert agent["mode"] == "image-pilot-qwen3-vl-4b" and agent["usage"]
    assert any(c.get("version_id") == state["curated"] for c in agent["citations"])
    calls = [json.loads(p.read_text(encoding="utf-8")) for p in (WORK / "lmstudio/predictions").glob("*.json")]
    durations = [c["duration_seconds"] for c in calls]
    result = {"passed": True, "preview_images_verified": len(display["images"]), "original_hashes_verified": len(originals),
              "exports": exports, "vision_calls": len(calls), "vision_total_seconds": round(sum(durations), 2),
              "vision_median_seconds": round(statistics.median(durations), 2),
              "agent_model": agent["mode"], "agent_duration_ms": agent["duration_ms"],
              "human_reviewed": 0, "browser_visual_test": read("browser-validation.json")
              if (WORK / "browser-validation.json").exists() else "not_run", "page_url": state["page_url"]}
    (WORK / "validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
