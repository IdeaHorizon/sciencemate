#!/usr/bin/env bash
# 预下载 KB embedding model（一次性，~120MB）。
#
# 同事 clone 仓库 + `pip install -e .` 后必跑一次。
# 之后离线可用；模型缓存到 ~/.cache/huggingface/。
#
# 中国大陆 / 网络慢：自动设 HF mirror（hf-mirror.com）。
# 想用别的 mirror：`HF_ENDPOINT=https://your-mirror bash scripts/install_embedding_model.sh`
# 离线机：先在有网机器跑，然后把 ~/.cache/huggingface tar 过去 + 设 HF_HOME 指向。
set -euo pipefail

# ── 默认 model：v0.3.2 KB embedding 用的中英多语 384 维小模型（~120MB）─────
MODEL_NAME="${HARNESS_EMBEDDING_MODEL:-intfloat/multilingual-e5-small}"

# ── HF mirror 兜底（不强制覆盖 user 已设的）──────────────────────────────
if [ -z "${HF_ENDPOINT:-}" ]; then
    # 默认设国内 mirror —— 海外用户想用官方 HF：先 `export HF_ENDPOINT=https://huggingface.co` 再跑
    export HF_ENDPOINT="https://hf-mirror.com"
fi

echo "════════════════════════════════════════════════════════════════════"
echo " Harness Framework · 预下载 KB embedding model"
echo "════════════════════════════════════════════════════════════════════"
echo "  model:        $MODEL_NAME"
echo "  endpoint:     $HF_ENDPOINT"
echo "  cache dir:    ${HF_HOME:-~/.cache/huggingface}"
echo "  expected size: ~120MB"
echo "════════════════════════════════════════════════════════════════════"
echo

# ── 检查 sentence-transformers 装了没 ─────────────────────────────────────
if ! python -c "import sentence_transformers" 2>/dev/null; then
    echo "❌ sentence-transformers 未装。先跑：" >&2
    echo "      pip install -e ." >&2
    echo "  或直接：" >&2
    echo "      pip install sentence-transformers" >&2
    exit 1
fi

# ── 真下载 + 跑一次 embed 验证 dim ────────────────────────────────────────
python - <<PYEOF
import os
import sys
import time

model_name = "$MODEL_NAME"
print(f"加载 {model_name}（首次会从 {os.environ['HF_ENDPOINT']} 下载）...")
t0 = time.time()
try:
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(model_name)
    dt = time.time() - t0
    dim = m.get_sentence_embedding_dimension()
    print(f"✓ 模型加载成功（耗时 {dt:.1f}s，dim={dim}）")

    print()
    print("跑 sanity smoke：embed 中 + 英短句...")
    vecs = m.encode(
        ["passage: dog is an animal", "passage: 狗是一种动物"],
        normalize_embeddings=True,
    )
    import numpy as np
    sim = float(np.dot(vecs[0], vecs[1]))
    print(f"  中英同义 cosine: {sim:.3f}")
    if sim < 0.5:
        print("  ⚠️  cosine 偏低（< 0.5）；可能 model 或 prefix 配置有问题。")
        sys.exit(2)
    print("  ✓ 中英语义对齐 OK")
except Exception as e:
    print(f"❌ 失败：{type(e).__name__}: {e}", file=sys.stderr)
    print(file=sys.stderr)
    print("常见故障：", file=sys.stderr)
    print("  1. 网络不通 / HF endpoint 不可达 → 试 HF_ENDPOINT=https://huggingface.co", file=sys.stderr)
    print("  2. 模型名错 → 默认 intfloat/multilingual-e5-small", file=sys.stderr)
    print("  3. 离线机 → 在有网机器跑完后拷 ~/.cache/huggingface/ + 设 HF_HOME", file=sys.stderr)
    sys.exit(1)
PYEOF

echo
echo "✅ Embedding model 预下载完成。可正常跑：python chat.py / python run_node.py"
echo
echo "提示："
echo "  - 跳过 semantic dedup（CI / 调试）：export HARNESS_DISABLE_SEMANTIC_DEDUP=1"
echo "  - 换 model：export HARNESS_EMBEDDING_MODEL=<huggingface_id> 再跑本脚本"
