"""Verify that the portable chat archive contains only public text."""

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from export_gpt_handoff import export_chat, sanitize_visible_text  # noqa: E402


def test_sanitize_visible_text_omits_credentials_and_client_context():
    text = (
        "正常问题\n"
        "password = example-private-value\n"
        "联系 person@example.com\n"
        "<in-app-browser-context source='ambient-ui-state'>hidden URL</in-app-browser-context>\n"
        "继续学习"
    )
    result = sanitize_visible_text(text)
    assert "正常问题" in result and "继续学习" in result
    assert "example-private-value" not in result
    assert "person@example.com" not in result
    assert "hidden URL" not in result


def test_export_chat_excludes_system_and_tool_data(tmp_path):
    session = tmp_path / "session.jsonl"
    events = [
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "private system instruction"}]}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "private tool output"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "如何上传？"}, {"type": "input_image", "image_url": "private-image"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "phase": "final_answer", "content": [{"type": "output_text", "text": "先建数据集。"}]}},
    ]
    session.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in events) + "\n", encoding="utf-8")
    target = tmp_path / "chat.md"
    counts = export_chat(session, target)
    archive = target.read_text(encoding="utf-8")
    assert counts == {"user": 1, "assistant": 1, "images_omitted": 1}
    assert "如何上传？" in archive and "先建数据集。" in archive
    assert "private system instruction" not in archive
    assert "private tool output" not in archive
    assert "private-image" not in archive
