import codecs
"""【学习标注：文件安全与基础质量】检查恶意文件、扩展名、真实格式、结构和敏感信息。"""
import csv
import hashlib
import json
import mimetypes
import re
import socket
import struct
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

SUPPORTED = {"txt", "md", "csv", "json", "jsonl", "png", "jpg", "jpeg", "webp", "wav", "mp3", "mp4", "webm", "pdf", "docx"}
TEXT = {"txt", "md", "csv", "json", "jsonl"}
DOCUMENTS = TEXT | {'pdf', 'docx'}
MODALITY_FORMATS = {
    'text': DOCUMENTS, 'tabular': {'csv','json','jsonl'},
    'image': DOCUMENTS | {'png','jpg','jpeg','webp'},
    'audio': DOCUMENTS | {'wav','mp3'}, 'video': DOCUMENTS | {'mp4','webm'},
    'multimodal': SUPPORTED,
}

def require_modality(modality, filename):
    from .common import require
    names={'text':'文本','tabular':'表格','image':'图像','audio':'音频','video':'视频','multimodal':'多模态'}
    extension=Path(filename).suffix.lower().lstrip('.')
    require(extension in MODALITY_FORMATS.get(modality,set()),
        f'文件格式 .{extension} 不符合{names.get(modality,modality)}数据集；请选择匹配的数据类型或多模态数据集',422)
# 数字段不得从资产 ID / SHA 的十六进制词中间截取，否则随机 ID 会偶发被当作手机号。
PII = re.compile(r"(?<![\da-fA-F])1[3-9]\d{9}(?![\da-fA-F])|(?<![\da-fA-F])\d{17}[\dXx](?![\da-fA-F])|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def inspect_asset(asset, storage, settings, heartbeat=lambda: None):
    """Stream original bytes once; never persist detected sensitive values in reports."""
    ext = Path(asset.filename).suffix.lower().lstrip(".")
    if ext not in SUPPORTED:
        return {"status": "rejected", "format": "unsupported", "reason": "首期尚未验证该格式", "coverage": 0}
    digest, length, sensitive = hashlib.sha256(), 0, False
    malware = False
    tail = b""
    sock = None
    if settings.scan_mode == "clamav":
        if asset.size > settings.malware_max_bytes:
            raise RuntimeError("文件超过病毒扫描容量；保持隔离，请拆分文件或配置更大的扫描服务")
        sock = socket.create_connection((settings.clamav_host, settings.clamav_port), timeout=30)
        sock.sendall(b"zINSTREAM\0")
    try:
        with tempfile.TemporaryFile() as staged:
            decoder = codecs.getincrementaldecoder("utf-8")("strict") if ext in TEXT else None
            for chunk in storage.chunks(asset.object_key):
                heartbeat()
                length += len(chunk)
                if length > asset.size:
                    return {"status": "rejected", "integrity": "failed", "coverage": 0}
                digest.update(chunk)
                staged.write(chunk)
                combined = tail + chunk
                malware |= b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in combined
                if decoder:
                    # Detection is a conservative rule check, not a complete DLP classifier.
                    sensitive |= bool(PII.search(combined.decode("utf-8", errors="ignore")))
                    decoder.decode(chunk)
                tail = combined[-256:]
                if sock:
                    sock.sendall(struct.pack(">I", len(chunk)) + chunk)
            if decoder:
                decoder.decode(b"", final=True)
            if sock:
                sock.sendall(b"\x00\x00\x00\x00")
                response = bytearray()
                while not response.endswith(b"\0") and len(response) < 4096:
                    block = sock.recv(1024)
                    if not block:
                        break
                    response.extend(block)
                result = bytes(response).decode(errors="replace").strip("\0\n")
                if "Heuristics.Limits.Exceeded" in result:
                    raise RuntimeError("病毒扫描达到容量或递归限制，检查不完整")
                if result.endswith(" FOUND"):
                    malware = True
                elif not result.endswith(" OK"):
                    raise RuntimeError("病毒检查服务未返回有效结果")
            valid_checksum = digest.hexdigest() == asset.sha256 and length == asset.size
            staged.seek(0)
            structure = inspect_format(staged, ext, length)
            rejected = not valid_checksum or malware or sensitive or structure.get("status") != "passed"
            return {"status": "rejected" if rejected else "clean", "integrity": "passed" if valid_checksum else "failed",
                    "malware": "detected" if malware else "passed",
                    "malware_method": settings.scan_mode, "sensitive": "flagged" if sensitive else "not_detected",
                    "sensitive_method": "text-regex-v2" if ext in TEXT else "not_checked_binary",
                    "structure": structure, "coverage": 1,
                    "media_type": mimetypes.guess_type(asset.filename)[0] or "application/octet-stream"}
    finally:
        if sock:
            sock.close()


def inspect_format(stream, ext, length):
    result = {"status": "passed", "rows": None, "empty_values": 0, "method": "structure-v1"}
    try:
        if ext in {"csv", "jsonl", "txt", "md"}:
            import io
            text = io.TextIOWrapper(stream, encoding="utf-8", newline="")
            try:
                rows = 0
                if ext == "csv":
                    reader = csv.reader(text, strict=True)
                    header = next(reader)
                    if not header:
                        raise ValueError("missing header")
                    for row in reader:
                        if len(row) != len(header):
                            raise ValueError("CSV column count mismatch")
                        rows += 1
                        result["empty_values"] += sum(not value.strip() for value in row)
                    result["columns"] = header[:100]
                else:
                    for line in text:
                        if len(line) > 8 * 1024**2:
                            raise ValueError("single record too large")
                        if ext == "jsonl" and line.strip():
                            item = json.loads(line)
                            if not isinstance(item, dict):
                                raise ValueError("JSONL must contain objects")
                            if "messages" in item and not (isinstance(item["messages"], list) and
                                all(isinstance(m, dict) and m.get("role") in {"system", "user", "assistant", "tool"}
                                    and isinstance(m.get("content"), str) for m in item["messages"])):
                                raise ValueError("invalid conversation annotation")
                        rows += bool(line.strip())
                result["rows"] = rows
            finally:
                text.detach()
        elif ext == "json":
            if length > 64 * 1024**2:
                return {"status": "not_checked", "reason": "JSON 超过 64MB，请使用流式 JSONL"}
            item = json.load(stream)
            result["rows"] = len(item) if isinstance(item, list) else 1
            if isinstance(item, dict) and "annotations" in item:
                if not isinstance(item["annotations"], list) or not isinstance(item.get("images"), list):
                    raise ValueError("invalid annotation structure")
                result["annotation"] = "coco-structure"
        elif ext in {"png", "jpg", "jpeg", "webp"}:
            with Image.open(stream) as image:
                if image.width * image.height > 40_000_000:
                    raise ValueError("image pixel limit exceeded")
                expected = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}[ext]
                if image.format != expected:
                    raise ValueError("image extension mismatch")
                result["dimensions"] = [image.width, image.height]
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("animated images are not supported")
                image.verify()
            # verify() checks containers; JPEG pixel data can still be truncated.
            # Reopen with the same bounded dimensions and decode without altering originals.
            stream.seek(0)
            with Image.open(stream) as decoded:
                decoded.load()
            result["method"] = "image-full-decode-v2"
        elif ext == "docx":
            with zipfile.ZipFile(stream) as archive:
                infos = archive.infolist()
                if len(infos) > 10000 or sum(i.file_size for i in infos) > 256 * 1024**2:
                    raise ValueError("document expansion limit exceeded")
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("invalid DOCX structure")
                if any(i.filename.lower().endswith("vbaproject.bin") for i in infos):
                    raise ValueError("macro content not supported")
                result["method"] = "docx-container"
        else:
            head = stream.read(32)
            signatures = {"pdf": head.startswith(b"%PDF-"), "wav": head[:4] == b"RIFF" and head[8:12] == b"WAVE",
                          "mp3": head[:3] == b"ID3" or (len(head) > 1 and head[0] == 255 and head[1] & 224 == 224),
                          "mp4": head[4:8] == b"ftyp", "webm": head[:4] == b"\x1aE\xdf\xa3"}
            if not signatures.get(ext, False):
                raise ValueError("file signature mismatch")
            result["method"] = "signature-only"
    except (ValueError, UnicodeError, csv.Error, StopIteration, OSError, zipfile.BadZipFile,Image.DecompressionBombError) as exc:
        result = {"status": "failed", "reason": type(exc).__name__}
    return result
