#!/usr/bin/env bash
# Pre-flight sanity check —— 跑真 LLM pipeline 前确认环境齐全。
#
# 跑：
#   bash scripts/sanity_check.sh
#
# 检查项：
#   1. Python deps（sentence-transformers / numpy / httpx 等）
#   2. .env 必填字段（LLM_API_KEY / LLM_BASE_URL / LLM_MODEL）
#   3. KB embedding model 已下载（不下走 substring fallback 也行但 dedup 弱）
#   4. LLM endpoint 通（quick chat ping）
#   5. LAMMPS binary 存在（如果要跑 pipeline）
#   6. pytest 全绿（不联网）
#   7. orchestrator harness 能加载

set -uo pipefail
err=0

bold='\033[1m'
red='\033[31m'
green='\033[32m'
yellow='\033[33m'
reset='\033[0m'

echo ""
echo "════════════════════════════════════════════════════════════════════"
echo " Harness Framework · Pre-flight Sanity Check"
echo "════════════════════════════════════════════════════════════════════"

# ── 1. Python deps ────────────────────────────────────────────────────────
echo ""
echo -e "${bold}[1/7] Python deps${reset}"
python - <<'PYEOF'
import sys
missing = []
for pkg in ("httpx", "yaml", "dotenv", "numpy", "sentence_transformers"):
    try:
        __import__(pkg)
    except ImportError:
        missing.append(pkg)
if missing:
    print(f"  ❌ 缺：{missing}（跑 `pip install -e .`）")
    sys.exit(1)
print(f"  ✓ 全部装好")
PYEOF
[ $? -ne 0 ] && err=$((err+1))

# ── 2. .env 字段 ──────────────────────────────────────────────────────────
echo ""
echo -e "${bold}[2/7] .env 必填字段${reset}"
if [ ! -f .env ]; then
    echo -e "  ${yellow}⚠️  .env 不存在${reset} —— `cp .env.example .env` 编辑后再来"
    err=$((err+1))
else
    python - <<'PYEOF'
import os
import sys
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path.cwd() / ".env")
need = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
missing = [k for k in need if not os.getenv(k)]
if missing:
    print(f"  ❌ .env 缺：{missing}")
    sys.exit(1)
print(f"  ✓ LLM_API_KEY: {os.getenv('LLM_API_KEY')[:6]}...{os.getenv('LLM_API_KEY')[-4:]}")
print(f"  ✓ LLM_BASE_URL: {os.getenv('LLM_BASE_URL')}")
print(f"  ✓ LLM_MODEL: {os.getenv('LLM_MODEL')}")
PYEOF
    [ $? -ne 0 ] && err=$((err+1))
fi

# ── 3. KB embedding model ───────────────────────────────────────────────
echo ""
echo -e "${bold}[3/7] KB embedding model（v0.3.2+）${reset}"
if [ "${HARNESS_DISABLE_SEMANTIC_DEDUP:-}" = "1" ]; then
    echo -e "  ${yellow}⚠️  HARNESS_DISABLE_SEMANTIC_DEDUP=1 —— 已显式禁用 semantic dedup，跳过${reset}"
else
    python - <<'PYEOF'
import os
import sys
try:
    from core.embeddings import SentenceTransformersClient
    c = SentenceTransformersClient()
    # 检查 cache 已有 model（不强加载，仅判文件）
    model = os.getenv("HARNESS_EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
    from pathlib import Path
    cache = Path(os.path.expanduser(os.getenv("HF_HOME", "~/.cache/huggingface")))
    # HF hub model cache: models--<org>--<repo>/
    safe = "models--" + model.replace("/", "--")
    has_model = any(cache.rglob(safe))
    if has_model:
        print(f"  ✓ {model} 已下载到 {cache}")
    else:
        print(f"  ⚠️  {model} 未下载到 {cache}")
        print(f"     运行 `bash scripts/install_embedding_model.sh` 预下载（~120MB）")
        print(f"     或临时跳过：`export HARNESS_DISABLE_SEMANTIC_DEDUP=1`")
        sys.exit(2)
except Exception as e:
    print(f"  ❌ embedding 模块加载失败：{e}")
    sys.exit(1)
PYEOF
    rc=$?
    [ $rc -eq 1 ] && err=$((err+1))
    [ $rc -eq 2 ] && echo -e "  ${yellow}（非阻塞 warning，可继续）${reset}"
fi

# ── 4. LLM endpoint ping ───────────────────────────────────────────────
echo ""
echo -e "${bold}[4/7] LLM endpoint quick ping${reset}"
python - <<'PYEOF'
import asyncio
import os
import sys
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path.cwd() / ".env")
if not all(os.getenv(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")):
    print("  - skip（.env 没配齐）")
    sys.exit(0)
from core.llm import LLMClient, LLMMessage
async def ping():
    c = LLMClient()
    try:
        resp = await c.chat(
            [LLMMessage(role="user", content="say 'OK'")],
            max_tokens=10, temperature=0,
        )
        text = (resp.content or "").strip()
        print(f"  ✓ LLM 回应 {len(text)} 字符（finish_reason={resp.finish_reason}）: {text[:60]}")
    except Exception as e:
        print(f"  ❌ LLM 调用失败：{type(e).__name__}: {str(e)[:200]}")
        sys.exit(1)
asyncio.run(ping())
PYEOF
[ $? -ne 0 ] && err=$((err+1))

# ── 5. LAMMPS binary（可选）────────────────────────────────────────────
echo ""
echo -e "${bold}[5/7] LAMMPS binary（如果要跑 pipeline e2e）${reset}"
LMP="${LMP_BINARY:-/opt/homebrew/bin/lmp_serial}"
if [ -x "$LMP" ]; then
    echo "  ✓ $LMP 可执行"
else
    if command -v lmp_serial >/dev/null 2>&1; then
        echo "  ✓ lmp_serial in PATH ($(which lmp_serial))"
    elif command -v lmp >/dev/null 2>&1; then
        echo "  ✓ lmp in PATH ($(which lmp))"
    else
        echo -e "  ${yellow}⚠️  没找到 lmp / lmp_serial。仅 LAMMPS pipeline e2e 需要；普通 chat 不影响。${reset}"
    fi
fi

# ── 6. pytest（不联网，快速）──────────────────────────────────────────
echo ""
echo -e "${bold}[6/7] pytest 全套（不联网，~1.7s）${reset}"
out=$(python -m pytest tests/ \
    --ignore=tests/run_lammps_pipeline.py \
    --ignore=tests/run_real_research.py \
    --ignore=tests/smoke_real_llm.py \
    --ignore=tests/smoke_quality_check_real_llm.py \
    -q 2>&1 | tail -1)
echo "  → $out"
if echo "$out" | grep -qE "^\.+$|passed"; then
    echo "  ✓ pytest 全绿"
else
    echo -e "  ${red}❌ 测试有失败${reset}"
    err=$((err+1))
fi

# ── 7. orchestrator harness ─────────────────────────────────────────────
echo ""
echo -e "${bold}[7/7] orchestrator harness 加载${reset}"
python - <<'PYEOF'
import sys
try:
    from core.bootstrap import bootstrap
    bootstrap()
    from core.loader import load_harness
    for n in ("_orchestrator", "_curator", "literature", "hypothesis", "analysis"):
        h = load_harness(n)
        print(f"  ✓ {n}: {len(h.tools)} tools")
except Exception as e:
    print(f"  ❌ {type(e).__name__}: {e}")
    sys.exit(1)
PYEOF
[ $? -ne 0 ] && err=$((err+1))

# ── 结论 ──────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════════"
if [ $err -eq 0 ]; then
    echo -e " ${green}✅ All checks passed. 可以跑 chat.py / run_node.py / pipeline。${reset}"
else
    echo -e " ${red}❌ $err 项失败。修完上面问题再跑。${reset}"
fi
echo "════════════════════════════════════════════════════════════════════"
echo ""
exit $err
