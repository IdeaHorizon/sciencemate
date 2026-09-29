#!/usr/bin/env bash
# 个人版验收：从零到第一条回复。见 personal_smoke.py 的文档。
set -euo pipefail
cd "$(dirname "$0")/../.."
exec uv run --project platform/backend python scripts/acceptance/personal_smoke.py "$@"
