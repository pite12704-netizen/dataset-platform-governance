"""【学习标注：模型状态】只检查模型 API；与网站健康状态分开，不输出密钥。"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import Settings


def configured_models(settings):
    entries = [("Metadata model", settings.model_url, settings.model_name, settings.model_api_key)]
    if settings.vision_enabled:
        entries.append(("Vision model", settings.vision_model_url or settings.model_url,
                        settings.vision_model_name or settings.model_name,
                        settings.vision_model_api_key or settings.model_api_key))
    return entries


def startup_ports(entries):
    """仅自动管理本项目已导入的本机模型；远程模型由其运维服务管理。"""
    ports = set()
    for _, url, name, _ in entries:
        parsed = urlsplit(url)
        if (name == "image-pilot-qwen3-vl-4b" and parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost"}
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and parsed.path.rstrip("/") == "/v1"):
            ports.add(parsed.port or 80)
    return sorted(ports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup-config", action="store_true")
    args = parser.parse_args()
    try:
        entries = configured_models(Settings(_env_file=os.environ.get('DP_SETTINGS_FILE', str(ROOT / '.env'))))
        if args.startup_config:
            print(json.dumps({"local_ports": startup_ports(entries)}))
            return 0
    except Exception as exc:
        print(json.dumps({"configuration_error": type(exc).__name__}))
        return 1
    rows = []
    for label, url, model, key in entries:
        row = {"service": label, "model": model, "status": "Not configured"}
        if url and model:
            started = time.monotonic()
            try:
                headers = {"Authorization": "Bearer " + key} if key else {}
                with httpx.Client(trust_env=False, timeout=3, follow_redirects=False) as client:
                    response = client.get(url.rstrip("/") + "/models", headers=headers)
                    response.raise_for_status()
                    listed = any(item.get("id") == model for item in response.json()["data"])
                # /models 可以包含未加载模型，不能将可发现误写成推理已验证。
                row["status"] = "API available; model listed" if listed else "Model not listed"
            except Exception as exc:
                row["status"] = type(exc).__name__
            row["seconds"] = round(time.monotonic() - started, 3)
        rows.append(row)
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
