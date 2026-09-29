#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

docker compose -f docker-compose.dev.yml up -d postgres redis

if [[ "${HARNESS_BRIDGE_ENABLED:-false}" == "true" ]]; then
  : "${HARNESS_ROOT:?Set HARNESS_ROOT when HARNESS_BRIDGE_ENABLED=true}"
  if [[ ! -f "$HARNESS_ROOT/core/agent_loop.py" ]]; then
    echo "HARNESS_ROOT is not a valid harness-framework checkout" >&2
    exit 1
  fi
  if [[ ! -x "$HARNESS_ROOT/.venv/bin/python" ]]; then
    uv venv "$HARNESS_ROOT/.venv"
  fi
  uv pip install --python "$HARNESS_ROOT/.venv/bin/python" -e "$HARNESS_ROOT"
  export HARNESS_PYTHON="${HARNESS_PYTHON:-$HARNESS_ROOT/.venv/bin/python}"
  export HARNESS_STATE_ROOT="${HARNESS_STATE_ROOT:-$repo_dir/backend/data/harness_runtime}"
fi

cd backend
uv sync --extra dev

export DEBUG=false
export LOCAL_DEMO_MODE=true
export SECRET_KEY="${SECRET_KEY:-atrium-local-demo-secret-change-before-deploying}"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/research_platform}"
export HARNESS_BRIDGE_ENABLED="${HARNESS_BRIDGE_ENABLED:-false}"

uv run alembic upgrade head
uv run python scripts/seed_local_demo.py --i-understand-this-is-local-demo
# `--timeout-graceful-shutdown`：兜底，不是主修（主修见 app/services/sse.py）。
# 排空阶段的上限由**我们**定，而不是由某条连接肯不肯结束来定 —— 将来有人写出
# 第三个长活循环并绕过 sse_response 时，最坏也只是等这么久，不会永远退不掉。
exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --timeout-graceful-shutdown 20
