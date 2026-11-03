#!/usr/bin/env bash
# 云端只建立代码学习/检查环境；当前业务服务继续运行在用户电脑上。
set -euo pipefail
cd "$(dirname "$0")/.."
python3 --version
python3 -m venv .venv-cloud
.venv-cloud/bin/python -m pip install --disable-pip-version-check --require-hashes -r backend/requirements.lock
npm --prefix frontend ci
printf '%s\n' 'Code environment prepared. Run: bash scripts/check-cloud.sh'
