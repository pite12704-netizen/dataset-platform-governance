#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
while IFS='=' read -r name _; do
  if [[ "$name" == DP_* && "$name" != DP_TEST_BROWSER_EXE ]]; then unset "$name"; fi
done < <(env)
# 不导入本机业务 .env，不运行 MySQL 集成测试、迁移或真实模型推理。
unset DP_TEST_DATABASE_URL DP_TEST_REDIS_URL DP_TEST_MYSQL DP_TEST_REDIS DP_REVIEW_TEST_URL DP_INTEGRATION_ENV DP_MODEL_API_KEY
unset DP_INGEST_TEST_DATABASE_URL DP_INGEST_TEST_S3_ENDPOINT DP_INGEST_TEST_S3_ACCESS DP_INGEST_TEST_S3_SECRET DP_INGEST_ISOLATED_RUN
export DP_ENVIRONMENT=test DP_TASK_MODE=inline DP_STORAGE_BACKEND=local
export DP_DATABASE_URL=sqlite:///./var/cloud-checks/isolated.db
export DP_MODEL_URL=http://127.0.0.1:1/v1 DP_VISION_MODEL_URL=http://127.0.0.1:1/v1
export DP_SECRET_KEY=cloud-checks-only-not-a-business-secret-0123456789
export DP_SETTINGS_FILE=/dev/null
export PYTHONPATH=backend:.
mkdir -p var/cloud-checks
.venv-cloud/bin/python -m ruff check backend/app scripts/audit_repository.py
.venv-cloud/bin/python -m pytest backend/tests scripts/tests
npm --prefix frontend run test:uploads
npm --prefix frontend run test:directories
npm --prefix frontend run test:tasks
npm --prefix frontend run test:agent
npm --prefix frontend run test:previews
npm --prefix frontend run build
npm --prefix frontend run test:browser
