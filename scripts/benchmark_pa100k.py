"""用 PA-100K 测试集评测平台正在使用的本地视觉模型。

【学习标注：模型评测】prepare 固定随机抽样并保存真值；run 复用 app.vision.predict，
逐张保存原始预测，支持中断续跑；report 同时报告覆盖率、类别不平衡基线和混淆指标。
原始图片仅用于本地评测，不自动进入正式数据集或发布流程。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "var" / "benchmarks" / "pa100k-2000"
DATASET = "tuandunghcmut/PA-100K"
SPLIT_SIZE = 10_000
DEFAULT_SEED = 20261001


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def prepare(root: Path, limit: int, seed: int, workers: int):
    root.mkdir(parents=True, exist_ok=True)
    images = root / "images"
    images.mkdir(exist_ok=True)
    manifest_path = root / "manifest.jsonl"
    parquet_path = root / "pa100k-test.parquet"
    if not parquet_path.exists():
        raise RuntimeError(f"缺少官方测试集文件：{parquet_path}")
    dependency_dir = root / "_deps"
    sys.path.insert(0, str(dependency_dir))
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError(f"缺少本地 Parquet 读取组件：{dependency_dir}") from exc
    table = parquet.read_table(parquet_path)
    if table.num_rows != SPLIT_SIZE:
        raise RuntimeError(f"测试集行数异常：期望 {SPLIT_SIZE}，实际 {table.num_rows}")
    selected = sorted(random.Random(seed).sample(range(SPLIT_SIZE), limit))
    values = table.take(selected).to_pylist()
    rows = []
    for position, (index, value) in enumerate(zip(selected, values), 1):
        image_value = value["image"]
        raw = image_value.get("bytes") if isinstance(image_value, dict) else None
        if not raw:
            raise RuntimeError(f"Parquet 第 {index} 行没有内嵌图片")
        name = f"{index:05d}-{Path(value['image_name']).name}"
        target = images / name
        target.write_bytes(raw)
        with Image.open(target) as image:
            width, height = image.size
            image.verify()
        rows.append({
            "row_idx": index, "image_name": value["image_name"],
            "path": target.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(), "width": width, "height": height,
            "truth": {"hat": bool(value["Hat"]), "short_sleeve": bool(value["ShortSleeve"]),
                      "long_sleeve": bool(value["LongSleeve"])},
        })
        if position % 200 == 0 or position == limit:
            print(f"prepare {position}/{limit}", flush=True)
    if len(rows) != limit:
        raise RuntimeError(f"样本不完整：期望 {limit}，实际 {len(rows)}")
    with manifest_path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    metadata = {
        "dataset": "PA-100K", "repository": DATASET, "split": "test",
        "split_size": SPLIT_SIZE, "sample_size": limit, "seed": seed,
        "license": "CC BY 4.0（依据 UPAR 官方仓库对 PA-100K 的许可说明）",
        "scored_attributes": ["hat", "sleeve"],
        "not_scored": {
            "upper_color": "PA-100K 没有上衣颜色真值",
            "gender": "平台视觉提示词明确不推断性别，且当前输出结构没有 gender 字段",
        },
    }
    (root / "source.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"prepared={len(rows)} manifest={manifest_path}")


def service_settings():
    values = {}
    path = ROOT / "deployment" / "service.env.local"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    url = values.get("DP_MODEL_URL", "http://127.0.0.1:1234/v1")
    url = url.replace("host.docker.internal", "127.0.0.1")
    return SimpleNamespace(
        model_url=url,
        model_name=values.get("DP_MODEL_NAME", "image-pilot-qwen3-vl-4b"),
        model_api_key=values.get("DP_MODEL_API_KEY", "local"),
        vision_enabled=True,
    )


def classify(result) -> tuple[dict | None, int, bool]:
    people = result.persons
    if len(people) != 1:
        return None, len(people), bool(result.truncated)
    return people[0].labels.model_dump(), 1, bool(result.truncated)


def run(root: Path, max_new: int | None):
    sys.path.insert(0, str(ROOT / "backend"))
    from app.vision import PROMPT_VERSION, predict

    manifest = read_jsonl(root / "manifest.jsonl")
    if not manifest:
        raise RuntimeError("请先运行 prepare")
    output = root / "predictions.jsonl"
    existing_rows = read_jsonl(output)
    existing = {row["row_idx"]: row for row in existing_rows}
    pending = [row for row in manifest if row["row_idx"] not in existing]
    if max_new is not None:
        pending = pending[:max_new]
    settings = service_settings()
    print(f"model={settings.model_name} prompt={PROMPT_VERSION} pending={len(pending)}", flush=True)
    completed = 0
    with output.open("a", encoding="utf-8") as stream:
        for sample in pending:
            record = {"row_idx": sample["row_idx"], "image_name": sample["image_name"],
                      "truth": sample["truth"], "model": settings.model_name,
                      "prompt_version": PROMPT_VERSION}
            error = None
            for attempt in range(3):
                try:
                    with Image.open(ROOT / sample["path"]) as source:
                        result, trace = predict(source.convert("RGB"), settings)
                    labels, persons, truncated = classify(result)
                    record.update(status="ok", prediction=labels, detected_persons=persons,
                                  truncated=truncated, trace=trace)
                    error = None
                    break
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:1000]
                    time.sleep(2**attempt)
            if error:
                record.update(status="error", prediction=None, detected_persons=None,
                              truncated=False, error=error)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            existing[record["row_idx"]] = record
            completed += 1
            if completed % 10 == 0 or completed == len(pending):
                print(f"run {completed}/{len(pending)} total={len(existing)}/{len(manifest)}", flush=True)
    report(root)


def safe_div(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def binary_metrics(truth: list[bool], prediction: list[bool | None]) -> dict:
    tp = sum(t and p is True for t, p in zip(truth, prediction))
    tn = sum(not t and p is False for t, p in zip(truth, prediction))
    fp = sum(not t and p is True for t, p in zip(truth, prediction))
    fn = sum(t and p is False for t, p in zip(truth, prediction))
    unresolved = sum(p is None for p in prediction)
    total = len(truth)
    tpr, tnr = safe_div(tp, tp + fn + sum(t and p is None for t, p in zip(truth, prediction))), \
               safe_div(tn, tn + fp + sum(not t and p is None for t, p in zip(truth, prediction)))
    return {
        "samples": total, "positive_rate": safe_div(sum(truth), total),
        "majority_baseline_accuracy": safe_div(max(sum(truth), total - sum(truth)), total),
        "coverage": safe_div(total - unresolved, total), "accuracy": safe_div(tp + tn, total),
        "balanced_accuracy": round((tpr + tnr) / 2, 6) if tpr is not None and tnr is not None else None,
        "precision": safe_div(tp, tp + fp), "recall": tpr,
        "f1": safe_div(2 * tp, 2 * tp + fp + fn + sum(t and p is None for t, p in zip(truth, prediction))),
        "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn, "unresolved": unresolved},
    }


def report(root: Path):
    predictions = read_jsonl(root / "predictions.jsonl")
    manifest = read_jsonl(root / "manifest.jsonl")
    samples = {row["row_idx"]: row for row in manifest}
    by_index = {row["row_idx"]: row for row in predictions}
    evaluated = [by_index[row["row_idx"]] for row in manifest if row["row_idx"] in by_index]
    hat_truth, hat_prediction = [], []
    sleeve_truth, sleeve_strict, sleeve_collapsed = [], [], []
    latencies, errors, one_person = [], 0, 0
    colors = {}
    for row in evaluated:
        prediction = row.get("prediction")
        if row.get("status") == "error":
            errors += 1
        if row.get("detected_persons") == 1:
            one_person += 1
        if row.get("trace", {}).get("seconds") is not None:
            latencies.append(float(row["trace"]["seconds"]))
        truth = row["truth"]
        hat_truth.append(bool(truth["hat"]))
        hat_prediction.append(None if not prediction or prediction["hat"] == "unclear"
                              else prediction["hat"] == "hat")
        if bool(truth["short_sleeve"]) != bool(truth["long_sleeve"]):
            short = bool(truth["short_sleeve"])
            sleeve_truth.append(short)
            value = prediction and prediction["sleeve"]
            sleeve_strict.append(True if value == "short_sleeves" else False if value == "long_sleeves" else None)
            sleeve_collapsed.append(True if value in {"short_sleeves", "sleeveless"}
                                    else False if value == "long_sleeves" else None)
        if prediction:
            colors[prediction["upper_color"]] = colors.get(prediction["upper_color"], 0) + 1
    result = {
        "dataset": "PA-100K/test", "requested_samples": len(manifest),
        "completed_samples": len(evaluated), "model": evaluated[0]["model"] if evaluated else None,
        "prompt_version": evaluated[0]["prompt_version"] if evaluated else None,
        "errors": errors, "exactly_one_person_rate": safe_div(one_person, len(evaluated)),
        "latency_seconds": {
            "mean": round(sum(latencies) / len(latencies), 3) if latencies else None,
            "p50": round(sorted(latencies)[len(latencies)//2], 3) if latencies else None,
            "total": round(sum(latencies), 3),
        },
        "metrics": {
            "hat": binary_metrics(hat_truth, hat_prediction) if hat_truth else {},
            "sleeve_strict": binary_metrics(sleeve_truth, sleeve_strict) if sleeve_truth else {},
            "sleeve_short_includes_sleeveless": binary_metrics(sleeve_truth, sleeve_collapsed) if sleeve_truth else {},
        },
        "unscored_upper_color_distribution": colors,
        "notes": [
            "PA-100K 没有上衣颜色真值，因此只记录预测分布，不计算颜色准确率。",
            "袖长只评估 ShortSleeve 与 LongSleeve 恰有一个为真的样本。strict 将 sleeveless 记为未覆盖；collapsed 将其并入短袖。",
            "准确率把模型错误、非单人输出和 unclear 作为未正确识别；同时单独报告 coverage。",
        ],
    }
    (root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    diagnostics = []
    for row in evaluated:
        truth, prediction, reasons = row["truth"], row.get("prediction"), []
        if row.get("status") == "error":
            reasons.append("model_request_error")
        if row.get("detected_persons") != 1:
            reasons.append("not_exactly_one_person")
        expected_hat = "hat" if truth["hat"] else "no_hat"
        if not prediction or prediction["hat"] != expected_hat:
            reasons.append("hat_mismatch")
        expected_sleeve = "short_sleeves" if truth["short_sleeve"] else "long_sleeves"
        compatible = {"short_sleeves", "sleeveless"} if truth["short_sleeve"] else {"long_sleeves"}
        if not prediction or prediction["sleeve"] not in compatible:
            reasons.append("sleeve_mismatch")
        if reasons:
            sample = samples[row["row_idx"]]
            diagnostics.append({"row_idx": row["row_idx"], "path": sample["path"], "reasons": reasons,
                                "truth": {"hat": expected_hat, "sleeve": expected_sleeve},
                                "prediction": prediction, "detected_persons": row.get("detected_persons")})
    with (root / "diagnostic_samples.jsonl").open("w", encoding="utf-8") as stream:
        for row in diagnostics:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def percent(value):
        return "—" if value is None else f"{value * 100:.2f}%"

    hat, sleeve, collapsed = result["metrics"]["hat"], result["metrics"]["sleeve_strict"], \
                             result["metrics"]["sleeve_short_includes_sleeveless"]
    lines = [
        "# PA-100K 本地视觉模型评测", "",
        f"- 完成：{len(evaluated)} / {len(manifest)}（固定随机种子 {DEFAULT_SEED}）",
        f"- 模型：{result['model'] or '—'}",
        f"- 提示词版本：{result['prompt_version'] or '—'}",
        f"- 推理接口错误：{errors}",
        f"- 恰好输出一个人物：{percent(result['exactly_one_person_rate'])}",
        f"- 平均推理耗时：{result['latency_seconds']['mean']} 秒/张", "",
        "## 结论", "",
        f"- 帽子：accuracy {percent(hat['accuracy'])}，低于始终预测“不戴帽子”的 {percent(hat['majority_baseline_accuracy'])}；"
        f"正类 recall 只有 {percent(hat['recall'])}，不能自动入库。",
        f"- 袖长：严格 accuracy {percent(sleeve['accuracy'])}，balanced accuracy {percent(sleeve['balanced_accuracy'])}，"
        f"短袖 recall {percent(sleeve['recall'])}；可用于生成候选，必须人工复核。",
        f"- 将无袖并入短袖后 accuracy 为 {percent(collapsed['accuracy'])}，结论没有实质变化。", "",
    ]
    for name, metric in result["metrics"].items():
        lines.extend([f"## {name}", "", f"- accuracy: {percent(metric.get('accuracy'))}",
                      f"- balanced_accuracy: {percent(metric.get('balanced_accuracy'))}",
                      f"- coverage: {percent(metric.get('coverage'))}", f"- precision: {percent(metric.get('precision'))}",
                      f"- recall: {percent(metric.get('recall'))}", f"- f1: {percent(metric.get('f1'))}",
                      f"- confusion: `{json.dumps(metric.get('confusion'), ensure_ascii=False)}`", ""])
    lines.extend(["## 评测边界", "", *[f"- {note}" for note in result["notes"]], "",
                  f"错误分析清单：`diagnostic_samples.jsonl`（{len(diagnostics)} 条至少含一个不匹配项）。"])
    (root / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "run", "report"])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--max-new", type=int)
    args = parser.parse_args()
    if not 1 <= args.limit <= SPLIT_SIZE:
        parser.error("limit 必须在 1 到 10000 之间")
    if args.command == "prepare":
        prepare(args.root.resolve(), args.limit, args.seed, args.workers)
    elif args.command == "run":
        run(args.root.resolve(), args.max_new)
    else:
        report(args.root.resolve())


if __name__ == "__main__":
    main()
