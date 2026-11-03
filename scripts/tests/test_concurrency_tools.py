from types import SimpleNamespace

import asyncio
import httpx

from scripts.benchmark_api import measure
from scripts.benchmark_api import percentile
from scripts.concurrency_budget import calculate


def test_budget_accounts_for_rollout_and_does_not_multiply_shared_gpu_quota():
    settings = SimpleNamespace(database_pool_size=10, database_max_overflow=10,
        api_thread_tokens=40, model_max_inflight=2, database_pool_timeout=5,
        database_async_pool_size=5, database_async_max_overflow=0)
    result = calculate(settings, api_processes=2, task_processes=3, migration_processes=1, replicas=2)
    assert result["database_connection_upper_bound"] == 240
    assert result["api_thread_tokens_total"] == 160
    assert result["model_inflight_per_shared_resource_group"] == 2


def test_small_latency_sample_has_defined_percentile_and_empty_sample_is_unavailable():
    assert percentile([], .95) is None
    assert percentile([.2, .001, .3, .1], .95) == 300


def test_benchmark_keeps_failed_and_limited_requests_in_report(monkeypatch):
    original = httpx.AsyncClient
    def respond(request):
        code = 200 if request.url.path == "/health/live" else 429 if request.url.path.endswith("config") else 503
        return httpx.Response(code, json={})
    monkeypatch.setattr("scripts.benchmark_api.httpx.AsyncClient", lambda **kwargs:
        original(transport=httpx.MockTransport(respond), **kwargs))
    result = asyncio.run(measure("http://testserver", 6, 2, True))
    assert result["successful"] == 2 and result["failed"] == 4
    assert result["paths"]["/api/v1/config"]["status_counts"] == {"429": 2}
    assert result["paths"]["/api/v1/datasets?limit=10"]["success_p95_ms"] is None
    assert result["production_capacity_verified"] is False
