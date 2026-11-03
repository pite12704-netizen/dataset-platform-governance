"""Create an audited source snapshot and a redacted, readable chat archive.

The output lives under ignored var/ and is never added to Git. This is an
archive of visible text, not a native ChatGPT/Codex conversation migration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

import audit_repository


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "var" / "cloud-handoff"
VISIBLE_ROLES = {"user", "assistant"}
PRIVATE_LINE = re.compile(
    r"(?i)(?:password|passwd|secret|api[_ -]?key|access[_ -]?key|"
    r"bearer|credential|token|验证码|一次性代码|密码|口令|密钥|登录凭据)"
)
EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
TOKEN = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{25,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{20,})"
)
SESSION_WRAPPERS = (
    re.compile(r"<in-app-browser-context\b[^>]*>.*?</in-app-browser-context>", re.S),
    re.compile(r"<environment_context\b[^>]*>.*?</environment_context>", re.S),
)
LOGIN_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")


def sanitize_visible_text(value: str) -> str:
    for wrapper in SESSION_WRAPPERS:
        value = wrapper.sub("[已省略客户端环境信息]", value)
    lines = []
    private_block = False
    for line in value.splitlines():
        if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            private_block = True
        if private_block:
            lines.append("[已省略密钥内容]" if not lines or lines[-1] != "[已省略密钥内容]" else "")
            if "-----END " in line and "PRIVATE KEY-----" in line:
                private_block = False
            continue
        if PRIVATE_LINE.search(line) or TOKEN.search(line) or LOGIN_CODE.search(line):
            lines.append("[已省略凭据信息或相关原文]")
        else:
            lines.append(EMAIL.sub("[邮箱已省略]", line))
    return "\n".join(lines)


def export_chat(session: Path, output: Path) -> dict:
    counts = {"user": 0, "assistant": 0, "images_omitted": 0}
    with session.open("r", encoding="utf-8") as source, output.open("w", encoding="utf-8") as dest:
        dest.write("# 本地 Codex 对话可见文字存档（已脱敏）\n\n")
        dest.write("这是供跨设备阅读的文字快照，不是原生可继续的对话。")
        dest.write("只收录用户消息和助手的公开回复；工具输出、系统消息、图像和凭据已省略。\n\n")
        for raw in source:
            event = json.loads(raw)
            if event.get("type") != "response_item":
                continue
            payload = event.get("payload") or {}
            if payload.get("type") != "message" or payload.get("role") not in VISIBLE_ROLES:
                continue
            role = payload["role"]
            phase = payload.get("phase")
            if role == "assistant" and phase not in {"commentary", "final_answer"}:
                continue
            pieces = []
            for item in payload.get("content", []):
                if item.get("type") in {"input_text", "output_text"}:
                    pieces.append(item.get("text", ""))
                elif item.get("type") == "input_image":
                    pieces.append("[图片附件已省略]")
                    counts["images_omitted"] += 1
            content = sanitize_visible_text("\n".join(pieces)).strip()
            if not content:
                continue
            counts[role] += 1
            label = "用户" if role == "user" else "助手"
            if phase == "commentary":
                label += " · 进度"
            dest.write(f"## {label} {counts[role]}\n\n{content}\n\n")
    return counts


def create_bundle(output: Path) -> dict:
    report = audit_repository.audit()
    if not report["passed"]:
        raise RuntimeError("Repository audit failed; source bundle was not generated.")
    hashes = {}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
        for rel in audit_repository.candidates():
            data = (ROOT / rel).read_bytes()
            hashes[rel] = hashlib.sha256(data).hexdigest()
            archive.writestr(rel, data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or set(archive.namelist()) != set(hashes):
            raise RuntimeError("Source archive verification failed.")
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, help="Read-only local Codex session JSONL")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    source_path = OUTPUT / "dataset-platform-source.zip"
    hashes = create_bundle(source_path)
    summary = {"source_files": len(hashes), "source_zip_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest()}
    (OUTPUT / "SHA256SUMS.json").write_text(json.dumps(hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for rel in ("README.md", "CODE_STUDY_GUIDE.md", "docs/cloud-handoff.md", "docs/cloud-development.md"):
        destination = OUTPUT / Path(rel).name
        destination.write_bytes((ROOT / rel).read_bytes())
    if args.session:
        if not args.session.is_file():
            parser.error("Session path does not exist.")
        summary["chat"] = export_chat(args.session, OUTPUT / "codex-visible-chat-redacted.md")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
