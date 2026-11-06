"""Explicit opt-in evaluation against an authorized API; never runs during ordinary startup.

Input JSON: {"cases":[{"kind":"knowledge","query":"...","relevant_ids":["document-id"]}]}
Image cases also need version_id and asset_id; catalog cases expect version IDs.
Provide a short-lived authorized bearer via DP_EVAL_BEARER; never write it in input/output.
"""
import argparse
import json
import math
import os
import statistics
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def metrics(actual, relevant, k):
    actual, relevant = list(dict.fromkeys(actual))[:k], set(relevant)
    if not relevant:
        return {"no_answer_correct": not actual}
    flags = [int(identifier in relevant) for identifier in actual]
    dcg = sum(value/math.log2(i+2) for i, value in enumerate(flags))
    ideal = sum(1/math.log2(i+2) for i in range(min(k, len(relevant))))
    return {"recall_at_k": len(set(actual) & relevant)/len(relevant), "precision_at_k": sum(flags)/k,
            "mrr": next((1/(i+1) for i, value in enumerate(flags) if value), 0), "ndcg_at_k": dcg/ideal}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--k", type=int, default=5, choices=range(1, 21))
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.username or url.password or url.query or url.fragment or not url.hostname or (
        url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})):
        parser.error("Use HTTPS or loopback HTTP, without embedded credentials/query")
    token = os.environ.get("DP_EVAL_BEARER")
    if not token:
        parser.error("Set DP_EVAL_BEARER to a short-lived authorized identity")
    if args.output.exists():
        parser.error("Choose a new output file; existing evidence is never overwritten")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        parser.error("Provide 1 to 100 labeled cases; this is not a load-test driver")
    if args.k > 5 and any(case.get('kind') == 'knowledge' for case in cases):
        parser.error("Knowledge answers currently expose at most 5 evidence chunks; use k<=5")
    evidence = []
    with httpx.Client(timeout=60, trust_env=False, follow_redirects=False, headers={"Authorization": "Bearer "+token}) as client:
        for case in cases:
            kind = case["kind"]
            path = {"knowledge": "/knowledge/ask", "catalog": "/catalog/search", "image": "/images/search"}[kind]
            payload = {"version_id": case["version_id"], "asset_id": case["asset_id"]} if kind == "image" else {"query": case["query"]}
            if kind != "knowledge":
                payload["limit"] = args.k
            started = time.monotonic()
            try:
                response = client.post(args.base_url.rstrip("/")+"/api/v1/retrieval"+path, json=payload)
                elapsed = round((time.monotonic()-started)*1000, 2)
                if response.status_code != 200:
                    evidence.append({"kind": kind, "status": response.status_code, "latency_ms": elapsed})
                    continue
                result = response.json()
                rows = result.get("knowledge_citations", []) if kind == "knowledge" else result.get("items", [])
                field = {"knowledge": "document_id", "catalog": "version_id", "image": "asset_id"}[kind]
                actual = [row[field] for row in rows]
                evidence.append({"kind": kind, "status": 200, "latency_ms": elapsed, "mode": result.get("mode"),
                                 "returned_ids": actual, **metrics(actual, case["relevant_ids"], args.k)})
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                evidence.append({"kind": kind, "status": "request_or_protocol_failed", "latency_ms": round((time.monotonic()-started)*1000, 2)})
    aggregate = {}
    for kind in ("knowledge", "catalog", "image"):
        rows = [r for r in evidence if r["kind"] == kind]
        if not rows:
            continue
        values = {key: statistics.mean(r[key] for r in rows if key in r) for key in (
            "recall_at_k", "precision_at_k", "mrr", "ndcg_at_k", "no_answer_correct") if any(key in r for r in rows)}
        aggregate[kind] = {"cases": len(rows), "failed": sum(r["status"] != 200 for r in rows), **values}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"k": args.k, "aggregate": aggregate, "cases": evidence,
        "scope": "Retrieval labels supplied by operator; no automatic semantic correctness certification"}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(evidence), "aggregate": aggregate}, ensure_ascii=False))


if __name__ == "__main__":
    main()
