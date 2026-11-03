"""只读取配置并计算连接预算；不连接数据库，不输出连接串或凭据。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import Settings


def calculate(settings, api_processes, task_processes, migration_processes, replicas=1):
    per_process = settings.database_pool_size + settings.database_max_overflow
    async_per_api = settings.database_async_pool_size + settings.database_async_max_overflow
    return {"api_processes": api_processes, "task_execution_processes": task_processes,
        "migration_processes": migration_processes, "replicas": replicas,
        "database_connections_per_process_upper_bound": per_process,
        "async_database_connections_per_api_upper_bound": async_per_api,
        "database_connection_upper_bound": ((api_processes + task_processes) * replicas + migration_processes) * per_process + api_processes * replicas * async_per_api,
        "api_thread_tokens_per_process": settings.api_thread_tokens,
        "api_thread_tokens_total": api_processes * replicas * settings.api_thread_tokens,
        "model_inflight_per_shared_resource_group": settings.model_max_inflight,
        "pool_wait_timeout_seconds": settings.database_pool_timeout,
        "note": "Configured ceiling, not measured load. Task pools are disposed after each task; beat is a producer. Include rollout overlap in replicas."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=ROOT / ".env.business.local")
    for name, default in (("api-processes", 2), ("task-processes", 3), ("migration-processes", 1), ("replicas", 1)):
        parser.add_argument("--" + name, type=int, default=default)
    parser.add_argument("--mysql-max-connections", type=int)
    parser.add_argument("--reserved-connections", type=int, default=20)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if min(args.api_processes, args.task_processes, args.migration_processes, args.reserved_connections) < 0 or args.replicas < 1:
        parser.error("Process counts must be nonnegative and replicas must be positive")
    settings = Settings(_env_file=args.env, environment="test")
    result = calculate(settings, args.api_processes, args.task_processes, args.migration_processes, args.replicas)
    if args.mysql_max_connections is not None:
        if args.mysql_max_connections < 1:
            parser.error("MySQL max_connections must be positive")
        result["mysql_max_connections"] = args.mysql_max_connections
        result["reserved_connections"] = args.reserved_connections
        result["budget_fits"] = result["database_connection_upper_bound"] + args.reserved_connections <= args.mysql_max_connections
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 2 if result.get("budget_fits") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
