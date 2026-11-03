"""COCO 图像试验：小样本下载、重复检验、人物级 LM Studio 视觉属性预测。

本脚本只读取公开数据，不上传图片给外部模型。模型输出是未校准的候选，
不能作为人工审核结论。COCO 人物框来自原始标注，不声称本脚本训练了检测器。
"""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import sys
import time
import zipfile

import httpx
import numpy as np
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "var" / "image-pilot"
MODEL_REPO = "Xenova/clip-vit-base-patch32"
HF = "https://hf-mirror.com"
COCO = "https://s3.amazonaws.com/images.cocodataset.org"
MODEL_SHA256 = "0898a3facfdb27f0a041e57649b4989cfd094e4a0040d6ae75ed69917dfc7328"


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, path, expected_hash=None):
    if path.is_file() and (not expected_hash or digest(path) == expected_hash):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".download")
    for attempt in range(3):
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
                r.raise_for_status()
                with temp.open("wb") as out:
                    for chunk in r.iter_bytes(1024 * 1024):
                        out.write(chunk)
            if expected_hash and digest(temp) != expected_hash:
                raise ValueError(f"Checksum mismatch: {path.name}")
            temp.replace(path)
            return
        except (httpx.HTTPError, ValueError):
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


class RemoteZip(io.RawIOBase):
    """通过 HTTP Range 读取 ZIP 中指定文件，避免下载全部 241 MiB 标注。"""
    def __init__(self, url):
        self.url = url
        self.client = httpx.Client(timeout=60, follow_redirects=True)
        head = self.client.head(url)
        head.raise_for_status()
        self.size = int(head.headers["content-length"])
        self.position = 0
        self.transferred = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = offset + (self.position if whence == 1 else self.size if whence == 2 else 0)
        if self.position < 0:
            raise ValueError("negative seek")
        return self.position

    def read(self, size=-1):
        end = self.size if size < 0 else min(self.size, self.position + size)
        if end <= self.position:
            return b""
        response = self.client.get(self.url, headers={"Range": f"bytes={self.position}-{end - 1}"})
        if response.status_code != 206 or len(response.content) != end - self.position:
            raise ValueError("Remote source did not honor the requested range")
        self.position = end
        self.transferred += len(response.content)
        return response.content

    def close(self):
        self.client.close()
        super().close()


def fetch_data(count=24):
    meta = WORK / "metadata"
    meta.mkdir(parents=True, exist_ok=True)
    required = ["instances_val2017.json", "captions_val2017.json"]
    if not all((meta / name).is_file() for name in required):
        print("Fetching only validation annotation ZIP entries...", flush=True)
        with RemoteZip(COCO + "/annotations/annotations_trainval2017.zip") as remote:
            with zipfile.ZipFile(remote) as archive:
                for name in required:
                    save_json(meta / name, json.loads(archive.read("annotations/" + name)))
            print(f"Annotation bytes fetched: {remote.transferred}", flush=True)
    annotations = load_json(meta / required[0])
    captions = defaultdict(list)
    for row in load_json(meta / required[1])["annotations"]:
        captions[row["image_id"]].append(row["caption"])
    people = defaultdict(list)
    for row in annotations["annotations"]:
        if row["category_id"] == 1:
            people[row["image_id"]].append(row)
    candidates = []
    for image in annotations["images"]:
        # License 4 is CC BY 2.0 in the upstream license dictionary. Preserve per-image provenance.
        persons = people[image["id"]]
        if image["license"] != 4 or not 1 <= len(persons) <= 4 or any(p["iscrowd"] for p in persons):
            continue
        # This is a pilot sampling criterion, not an automatic deletion policy for production data.
        if any(p["bbox"][2] < 65 or p["bbox"][3] < 95 for p in persons):
            continue
        text = " ".join(captions[image["id"]]).lower()
        priority = sum(word in text for word in ("hat", "shirt", "wearing", "baseball", "woman", "man"))
        candidates.append((priority, image, persons))
    candidates.sort(key=lambda x: (-x[0], x[1]["id"]))
    singles = [x for x in candidates if len(x[2]) == 1][:count // 2]
    multiple = [x for x in candidates if len(x[2]) > 1][:count - len(singles)]
    chosen = singles + multiple
    if len(chosen) != count:
        raise ValueError(f"Not enough eligible images: {len(chosen)}")
    license_row = next(x for x in annotations["licenses"] if x["id"] == 4)
    records = []
    for _, row, persons in chosen:
        records.append({"id": str(row["id"]), "filename": row["file_name"],
            "width": row["width"], "height": row["height"], "license": license_row,
            "source_url": COCO + "/val2017/" + row["file_name"],
            "flickr_url": row.get("flickr_url"), "person_count": len(persons),
            "persons": [{"id": str(p["id"]), "bbox": p["bbox"]} for p in persons],
            "captions_for_sampling_only": captions[row["id"]]})
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda row: download(row["source_url"], WORK / "originals" / row["filename"]), records))
    for row in records:
        path = WORK / "originals" / row["filename"]
        with Image.open(path) as image:
            image.verify()
        row["sha256"] = digest(path)
    save_json(WORK / "source_manifest.json", {"dataset": "COCO 2017 validation subset", "images": records,
        "sampling": "12 single-person and 12 multi-person photos; non-random pilot biased toward clothing captions",
        "terms_url": "https://cocodataset.org/#termsofuse", "image_license": license_row,
        "annotations_license": "CC-BY-4.0", "source_annotations": COCO + "/annotations/annotations_trainval2017.zip",
        "note": "COCO provides person boxes, not hat/sleeve ground truth. Source captions are never model inputs."})
    print(f"Downloaded {len(records)} originals, {sum(x['person_count'] for x in records)} annotated persons", flush=True)


def fetch_model():
    directory = WORK / "model"
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.get(f"{HF}/api/models/{MODEL_REPO}")
        response.raise_for_status()
        metadata = response.json()
    revision = metadata["sha"]
    save_json(directory / "repository.json", {"repository": MODEL_REPO, "revision": revision,
        "download_mirror": HF, "model_sha256": MODEL_SHA256})
    for name in ["tokenizer.json", "tokenizer_config.json", "preprocessor_config.json", "config.json", "README.md"]:
        download(f"{HF}/{MODEL_REPO}/resolve/{revision}/{name}", directory / name)
    print("Downloading pinned 147 MiB quantized vision/text model...", flush=True)
    download(f"{HF}/{MODEL_REPO}/resolve/{revision}/onnx/model_quantized.onnx", directory / "model.onnx", MODEL_SHA256)
    print("Model SHA-256 verified", flush=True)


def phash(image):
    """32×32 灰度图的低频 DCT 哈希；仅作近重复候选召回。"""
    pixels = np.asarray(image.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    dct = np.cos(np.pi * (2 * np.arange(32) + 1)[None, :] * np.arange(8)[:, None] / 64)
    low = (dct @ pixels @ dct.T).flatten()[1:]
    return low > np.median(low)


def image_pair_stats(first, second):
    with Image.open(first) as a, Image.open(second) as b:
        hamming = int(np.count_nonzero(phash(a) != phash(b)))
        aa = np.asarray(a.convert("RGB").resize((64, 64)), dtype=np.float32) / 255
        bb = np.asarray(b.convert("RGB").resize((64, 64)), dtype=np.float32) / 255
        error = float(np.mean(np.abs(aa - bb)))
        aspect = abs(math.log((a.width / a.height) / (b.width / b.height)))
    return hamming, error, aspect


def deduplicate(inputs):
    """稳定代表图比较，不做传递闭包；原图始终保留，近重复只提出排除建议。"""
    kept, links = [], []
    hashes = {}
    for entry in inputs:
        path = WORK / entry["relative_path"]
        sha = digest(path)
        if sha in hashes:
            links.append({"filename": entry["filename"], "keep": hashes[sha], "kind": "exact",
                          "distance": 0, "pixel_mae": 0, "decision": "exclude_from_trial_export"})
            continue
        match = None
        for candidate in kept:
            dist, error, aspect = image_pair_stats(path, WORK / candidate["relative_path"])
            if dist <= 6 and error <= .035 and aspect < .01:
                match = {"filename": entry["filename"], "keep": candidate["filename"], "kind": "near",
                         "distance": dist, "pixel_mae": round(error, 5), "decision": "proposed_exclusion"}
                break
        if match:
            links.append(match)
        else:
            kept.append(entry)
        hashes[sha] = entry["filename"] if not match else match["keep"]
    return kept, links


ATTRIBUTES = {
    "hat": [("hat", "a photo of a person wearing a hat or a cap"),
            ("no_hat", "a photo of a person with a bare head and no hat"),
            ("unclear", "a photo of a person whose head is hidden or unclear")],
    "sleeve": [("short_sleeves", "a photo of a person wearing a short sleeved shirt or T-shirt"),
               ("long_sleeves", "a photo of a person wearing a long sleeved shirt or jacket"),
               ("sleeveless", "a photo of a person wearing a sleeveless top"),
               ("unclear", "a photo of a person whose upper clothing is hidden or unclear")],
    "upper_color": [(color, f"a photo of a person wearing a {color} shirt or top")
                    for color in ["white", "black", "red", "blue", "green", "yellow"]] +
                   [("other_or_unclear", "a photo of a person with multicolored, other colored or unclear upper clothing")],
}


class AttributeModel:
    def __init__(self):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.directory = WORK / "model"
        settings = load_json(self.directory / "preprocessor_config.json")
        self.mean = np.array(settings["image_mean"], dtype=np.float32)
        self.std = np.array(settings["image_std"], dtype=np.float32)
        self.tokenizer = Tokenizer.from_file(str(self.directory / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=77)
        self.tokenizer.enable_padding(length=77, pad_id=49407, pad_token="<|endoftext|>")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        self.session = ort.InferenceSession(str(self.directory / "model.onnx"), sess_options=options,
                                           providers=["CPUExecutionProvider"])
        self.groups, prompts = {}, []
        for key, candidates in ATTRIBUTES.items():
            self.groups[key] = (len(prompts), len(prompts) + len(candidates))
            prompts += [p for _, p in candidates]
        encoded = self.tokenizer.encode_batch(prompts)
        self.tokens = np.array([x.ids for x in encoded], dtype=np.int64)
        self.mask = np.array([x.attention_mask for x in encoded], dtype=np.int64)
        print("ONNX inputs:", [(x.name, x.shape) for x in self.session.get_inputs()], flush=True)
        print("ONNX outputs:", [(x.name, x.shape) for x in self.session.get_outputs()], flush=True)

    def predict(self, image):
        # Letterbox preserves the full head/torso of tall person crops for attribute inspection.
        canvas = ImageOps.pad(image.convert("RGB"), (224, 224), Image.Resampling.BICUBIC, color=(127, 127, 127))
        array = (np.asarray(canvas, dtype=np.float32) / 255 - self.mean) / self.std
        available = {"input_ids": self.tokens, "attention_mask": self.mask,
                     "pixel_values": array.transpose(2, 0, 1)[None].astype(np.float32)}
        values = self.session.run(None, {x.name: available[x.name] for x in self.session.get_inputs()})
        outputs = dict(zip([x.name for x in self.session.get_outputs()], values))
        logits = outputs["logits_per_image"][0]
        result = {}
        for key, (start, end) in self.groups.items():
            group = logits[start:end].astype(np.float64)
            probability = np.exp(group - group.max())
            probability /= probability.sum()
            order = np.argsort(probability)[::-1]
            label = ATTRIBUTES[key][int(order[0])][0]
            # Scores are relative prompt preferences, never calibrated accuracy/confidence.
            margin = float(probability[order[0]] - probability[order[1]])
            result[key] = {"label": label if margin >= .2 else "unclear", "top_candidate": label,
                           "relative_score": round(float(probability[order[0]]), 4),
                           "margin": round(margin, 4), "status": "pending_human_review"}
        return result


class LMStudioModel:
    """只连接本机 LM Studio；结构化图片分析失败必须显示失败，不能冒充阴性标签。"""
    def __init__(self):
        self.identifier = "image-pilot-qwen3-vl-4b"
        self.client = httpx.Client(base_url="http://127.0.0.1:1234/v1", timeout=180, trust_env=False)
        response = self.client.get("/models")
        response.raise_for_status()
        if self.identifier not in [m["id"] for m in response.json()["data"]]:
            raise ValueError("The pilot vision model is not loaded in LM Studio")
        self.cache = WORK / "lmstudio" / "predictions"
        self.cache.mkdir(exist_ok=True)
        self.prompt = (
            "Inspect only the provided person crop. Describe visible clothing, not identity. "
            "For hat use hat (hat or cap), no_hat (clearly bare head), or unclear (head hidden). "
            "For sleeve use short_sleeves, long_sleeves, sleeveless, or unclear. "
            "For upper_color use white, black, red, blue, green, yellow, other_or_unclear. "
            "If the relevant body part is outside the crop or occluded, choose unclear. "
            "Do not infer sex, gender identity, age or other personal characteristics. "
            "Return exactly one JSON object with keys hat, sleeve, upper_color; no explanation."
        )
        self.schema = {"type": "object", "properties": {
            key: {"type": "string", "enum": [x[0] for x in options]} for key, options in ATTRIBUTES.items()},
            "required": list(ATTRIBUTES), "additionalProperties": False}

    def predict(self, image):
        image = image.convert("RGB")
        image.thumbnail((768, 768), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        content = buffer.getvalue()
        weight_hash = load_json(WORK / "lmstudio" / "download_manifest.json")["files"]["Qwen3-VL-4B-Instruct-Q4_K_M.gguf"]["sha256"]
        cache_key = hashlib.sha256(content + self.prompt.encode() + weight_hash.encode()).hexdigest()
        path = self.cache / (cache_key + ".json")
        if path.exists():
            return load_json(path)["attributes"]
        started = time.monotonic()
        response = self.client.post("/chat/completions", json={"model": self.identifier,
            "temperature": 0, "max_tokens": 160, "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "person_clothing", "strict": True, "schema": self.schema}},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": self.prompt},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(content).decode()}}]}]})
        response.raise_for_status()
        body = response.json()
        values = json.loads(body["choices"][0]["message"]["content"])
        if set(values) != set(ATTRIBUTES):
            raise ValueError("Model output does not match the attribute schema")
        result = {}
        for key, value in values.items():
            if value not in self.schema["properties"][key]["enum"]:
                raise ValueError(f"Invalid model attribute: {key}")
            result[key] = {"label": value, "status": "pending_human_review", "relative_score": None,
                           "method": "lmstudio-vision-json"}
        save_json(path, {"attributes": result, "model": self.identifier, "weight_sha256": weight_hash,
                         "prompt": self.prompt, "usage": body.get("usage"),
                         "duration_seconds": round(time.monotonic() - started, 3)})
        return result


def prepare_inputs():
    start = time.monotonic()
    source = load_json(WORK / "source_manifest.json")
    originals = source["images"]
    inputs = [{"filename": x["filename"], "relative_path": "originals/" + x["filename"],
               "source_id": x["id"], "fixture": False} for x in originals]
    fixtures = WORK / "fixtures"
    fixtures.mkdir(exist_ok=True)
    expected = []
    # Add explicit test fixtures, never silently manipulate source pictures.
    for index, row in enumerate(originals[:4]):
        origin = WORK / "originals" / row["filename"]
        exact_name = "exact_" + row["filename"]
        shutil.copyfile(origin, fixtures / exact_name)
        near_name = "reencoded_" + row["filename"]
        with Image.open(origin) as image:
            image.convert("RGB").save(fixtures / near_name, quality=83)
        for filename, kind in [(exact_name, "exact"), (near_name, "near")]:
            inputs.append({"filename": filename, "relative_path": "fixtures/" + filename,
                           "source_id": row["id"], "fixture": True})
            expected.append({"filename": filename, "keep": row["filename"], "kind": kind})
    kept, duplicates = deduplicate(inputs)
    expected_pairs = {(x["filename"], x["keep"], x["kind"]) for x in expected}
    actual_pairs = {(x["filename"], x["keep"], x["kind"]) for x in duplicates}
    fixture_results = {"expected_duplicate_pairs": len(expected_pairs),
        "recovered_pairs": len(expected_pairs & actual_pairs),
        "missed_pairs": [list(x) for x in sorted(expected_pairs - actual_pairs)],
        "other_proposed_pairs": [list(x) for x in sorted(actual_pairs - expected_pairs)],
        "scope": "Controlled exact/re-encoding fixtures only; not an estimate of production accuracy"}
    save_json(WORK / "dedup_test.json", {"inputs": inputs, "kept": kept, "duplicates": duplicates,
                                        "test": fixture_results})
    print("Dedup fixture test:", json.dumps(fixture_results), flush=True)
    return start, source, originals, inputs, kept, duplicates, fixture_results


def analyze(backend="lmstudio"):
    start, source, originals, inputs, kept, duplicates, fixture_results = prepare_inputs()
    model = LMStudioModel() if backend == "lmstudio" else AttributeModel()
    analyzed = []
    retained_names = {x["filename"] for x in kept}
    retained_originals = [r for r in originals if r["filename"] in retained_names]
    for index, row in enumerate(retained_originals):
        record = dict(row)
        record["persons"] = []
        with Image.open(WORK / "originals" / row["filename"]) as image:
            for person in row["persons"]:
                x, y, w, h = person["bbox"]
                cropped = image.crop((max(0, int(x)), max(0, int(y)), min(image.width, math.ceil(x + w)),
                                      min(image.height, math.ceil(y + h))))
                record["persons"].append({**person, "attributes": model.predict(cropped),
                    "box_source": "COCO ground-truth annotation", "review_status": "pending_human_review"})
        analyzed.append(record)
        print(f"Attributes: {index + 1}/{len(retained_originals)} images", flush=True)
    # Joint filtering must match attributes on the SAME person, not two different people in an image.
    filters = {
        "hat": {"hat": "hat"},
        "short_sleeves": {"sleeve": "short_sleeves"},
        "hat_and_short_sleeves": {"hat": "hat", "sleeve": "short_sleeves"},
    }
    selections = {key: select_images(analyzed, criteria) for key, criteria in filters.items()}
    metadata = ({**load_json(WORK / "lmstudio" / "download_manifest.json"),
                 "runtime": "LM Studio / Qwen3-VL-4B / local API", "identifier": model.identifier,
                 "prompt": model.prompt, "input": "COCO person crops, maximum 768x768, no captions",
                 "score_semantics": "No calibrated confidence score; all labels require human review"}
                if backend == "lmstudio" else
                {**load_json(WORK / "model" / "repository.json"), "runtime": "ONNX Runtime / CPU",
                  "input": "Person crops using COCO boxes; no captions or labels supplied to model",
                  "preprocessing": "224x224 letterbox, bicubic, CLIP mean/std",
                  "score_semantics": "Uncalibrated relative preference among fixed prompts",
                  "attributes": ATTRIBUTES, "abstain_margin": .2})
    report = {"schema_version": 1, "title": "COCO 人物图像去重与属性试验", "source": source,
        "model": metadata,
        "summary": {"original_images": len(originals), "input_images_with_fixtures": len(inputs),
                    "single_person_images": sum(x["person_count"] == 1 for x in originals),
                    "multi_person_images": sum(x["person_count"] > 1 for x in originals),
                    "persons": sum(x["person_count"] for x in originals), "kept_images": len(kept),
                    "exact_duplicates": sum(x["kind"] == "exact" for x in duplicates),
                    "near_duplicate_candidates": sum(x["kind"] == "near" for x in duplicates),
                    "human_reviewed": 0, "elapsed_seconds": round(time.monotonic() - start, 2)},
        "inputs": inputs, "duplicates": duplicates, "duplicate_fixture_test": fixture_results,
        "images": analyzed, "selections": selections,
        "limitations": ["COCO 没有帽子、袖长和上衣颜色的对应真值，本次不报告属性准确率。",
            "使用 COCO 已有人物框，不代表已完成新图片的人物自动检测。",
            "所有属性均为模型候选，尚未经过人类复核；低分与遮挡需人工判断。",
            "暂不自动推断人物性别；这类外观标签需单独定义规范和人工复核。",
            "小样本定向抽样及受控重复副本不能证明大规模近重复检出率。"]}
    save_json(WORK / "report.json", report)
    exports = WORK / "exports"
    exports.mkdir(exist_ok=True)
    image_by_name = {row["filename"]: row for row in analyzed}
    for key, filenames in {"deduplicated": [x["filename"] for x in kept], **selections}.items():
        manifest = {"status": "experimental_pending_human_review", "filter": key,
                    "images": [image_by_name[x] for x in filenames if x in image_by_name],
                    "source": source["terms_url"], "model": report["model"]}
        save_json(exports / f"{key}.json", manifest)
        with zipfile.ZipFile(exports / f"{key}.zip", "w", zipfile.ZIP_STORED) as archive:
            archive.write(exports / f"{key}.json", "manifest.json")
            for name in filenames:
                # Fixtures excluded by deduplication; never substitute original bytes for a retained fixture.
                item = next(x for x in inputs if x["filename"] == name)
                archive.write(WORK / item["relative_path"], "images/" + name)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    print("Controlled duplicate test:", json.dumps(fixture_results, ensure_ascii=False), flush=True)


def select_images(images, criteria):
    """组合属性须由同一个人物满足，防止多人图片交叉匹配产生假阳性。"""
    return [r["filename"] for r in images if any(
        all(p["attributes"][key]["label"] == value for key, value in criteria.items())
        for p in r["persons"])]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["fetch-data", "fetch-model", "dedup", "analyze"])
    parser.add_argument("--backend", choices=["lmstudio", "clip"], default="lmstudio")
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    {"fetch-data": fetch_data, "fetch-model": fetch_model, "dedup": prepare_inputs,
     "analyze": lambda: analyze(args.backend)}[args.stage]()
