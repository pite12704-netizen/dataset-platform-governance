"""只读 API 小规模容量检查；保留 429/503 和失败计数，不把结果当上线容量证明。"""
import argparse
import asyncio
import json
import math
import os
import ssl
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

PATHS = ("/health/live", "/api/v1/config", "/api/v1/datasets?limit=10")


def percentile(values, fraction):
    return round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] * 1000, 2) if values else None


async def measure(base_url, requests, concurrency, verify, token=None):
    gate = asyncio.Semaphore(concurrency)
    headers = {"Authorization": "Bearer " + token} if token else {}
    rows = []
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=base_url, headers=headers, verify=verify, trust_env=False,
            follow_redirects=False, timeout=15, limits=httpx.Limits(max_connections=concurrency,
            max_keepalive_connections=concurrency)) as client:
        async def call(index):
            path = PATHS[index % len(PATHS)]
            async with gate:
                before = time.perf_counter()
                try:
                    response = await client.get(path)
                    status = str(response.status_code)
                except httpx.HTTPError as exc:
                    status = type(exc).__name__  # 不输出可能含凭据的异常文本。
                rows.append({"path": path, "status": status, "seconds": time.perf_counter() - before})
        await asyncio.gather(*(call(index) for index in range(requests)))
    elapsed = time.perf_counter() - started
    groups = {}
    for path in PATHS:
        samples = [row for row in rows if row["path"] == path]
        success = [row["seconds"] for row in samples if row["status"].startswith("2")]
        groups[path] = {"status_counts": dict(Counter(row["status"] for row in samples)),
                       "success_p50_ms": percentile(success, .5), "success_p95_ms": percentile(success, .95)}
    successful = sum(row["status"].startswith("2") for row in rows)
    return {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "requests": requests,
        "concurrency": concurrency, "authenticated": bool(token), "elapsed_seconds": round(elapsed, 3),
        "successful": successful, "failed": requests - successful,
        "successful_requests_per_second": round(successful / elapsed, 2), "paths": groups,
        "scope": "read_only_smoke_test", "production_capacity_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--report", type=Path, default=Path("var/operations/api-benchmark.json"))
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        parser.error("Provide the platform origin without credentials, path, query or fragment")
    if not 1 <= args.concurrency <= 64 or not 1 <= args.requests <= 10000:
        parser.error("concurrency must be 1..64; requests must be 1..10000")
    verify = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file else True
    result = asyncio.run(measure(args.base_url.rstrip("/"), args.requests, args.concurrency, verify,
                                os.environ.get("DP_BENCHMARK_TOKEN")))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
