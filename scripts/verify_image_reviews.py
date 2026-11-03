"""把真实 COCO 试验接入复核队列并测试整图定位；绝不模拟真实人员的复核通过。

仅适用于已经生成 var/image-pilot 的本机开发环境。真实授权回收、自审和导出测试在
backend/tests/test_image_reviews.py 的隔离数据库完成，避免污染正式复核记录。
"""
import json
import time
from pathlib import Path

from verify_image_pilot import RetryClient

WORK = Path(__file__).resolve().parents[1] / "var/image-pilot"


def read(name):
    return json.loads((WORK / name).read_text(encoding="utf-8"))


def main():
    state, accounts = read("platform-state.json"), read("test-accounts.json")
    client = RetryClient(base_url="http://127.0.0.1:8000/api/v1", trust_env=False, timeout=600,
                         headers={"X-Requested-With": "dataset-platform"})
    client.post("/auth/login", json={k: accounts["owner"][k] for k in ("email", "password")})
    version = client.get("/versions/" + state["curated"]).json()
    report = next(a for a in version["assets"] if a["filename"] == "image_pilot_report.json")
    imported = client.post("/image-reviews/import", json={"report_asset_id": report["id"]}).json()
    repeated = client.post("/image-reviews/import", json={"report_asset_id": report["id"]}).json()
    assert imported["id"] == repeated["id"] and imported["total"] == 41
    started = time.monotonic()
    job = client.post("/jobs", json={"version_id": version["id"], "kind": "vision",
                      "idempotency_key": "image-pilot-grounding-v1"}).json()
    deadline = time.monotonic() + 600
    while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(3)
        job = client.get("/jobs/" + job["id"]).json()
    assert job["status"] == "completed", {k: job.get(k) for k in ("id", "status", "error")}
    detected = client.get("/image-reviews/" + job["result"]["batch_id"]).json()
    result = {"passed": True, "import_batch_id": imported["id"], "import_persons": imported["total"],
              "vision_job_id": job["id"], "vision_batch_id": detected["id"], "images": detected["provenance"]["images"],
              "detected_persons": detected["total"], "pending_persons": detected["counts"]["pending"],
              "model_seconds": round(sum(s["seconds"] for s in detected["provenance"]["scans"]), 3),
              "human_reviewed": detected["counts"]["approved"] + imported["counts"]["approved"],
              "wall_seconds": round(time.monotonic() - started, 3),
              "review_url": "http://127.0.0.1:5173/image-reviews/" + imported["id"],
              "vision_url": "http://127.0.0.1:5173/image-reviews/" + detected["id"]}
    # 这里只记录候选与已知人物框的几何匹配，不将服饰预测当作标签真值。
    truth = read("image_pilot_report.json")["images"]
    def iou(a, b):
        x = max(0, min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0]))
        y = max(0, min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))
        inter = x*y
        return inter/(a[2]*a[3]+b[2]*b[3]-inter)
    matches = 0
    for image in truth:
        expected = [p["bbox"] for p in image["persons"]]
        predicted = [p["predicted"]["bbox"] for p in detected["items"] if p["asset_id"] == image["asset_id"]]
        candidates = sorted([(iou(a,b), i,j) for i,a in enumerate(expected) for j,b in enumerate(predicted)], reverse=True)
        used_a, used_b = set(), set()
        for score, i, j in candidates:
            if score >= .5 and i not in used_a and j not in used_b:
                matches += 1
                used_a.add(i); used_b.add(j)
    result["grounding_diagnostic"] = {"method": "greedy_one_to_one_iou_0.5", "matched": matches,
        "reference_persons": 41, "predictions": detected["total"],
        "limitations": "仅这 24 张选择样本的定位诊断；不代表泛化准确率或服饰属性准确率"}
    (WORK / "review-validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
