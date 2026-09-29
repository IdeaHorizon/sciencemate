"""KB 复利 e2e dogfood —— 用真 embedding model + mock LLM 跑端到端。

目的：验证 Phase A-H 在**真实 sentence-transformers** 下端到端工作，看真实场景下：
  - semantic dedup 真模型对"字面不同语义同"的 claim 命中率如何
  - context engine 注入用 semantic 真排序，跟以前 substring 差多少
  - dreaming scheduler 在累积场景下何时触发
  - curator scan_artifact_disagreements 对真 LLM artifact 文本召回率

原来还有一条「mechanical thresholds 在真增长 KB 中是否 block 该 block 的」。
那些写入阈值闸已在 KB 两层重构里删除（它们教模型改 claim_type 过门，而类型是
晋升分道的路由键）。准入现在只在**晋升侧**，见 core/kb_promotion.py 与
scripts/replay_kb_two_tiers.py。

**不烧 LLM token** —— 用 mock LLM 输出预制 artifact + claim 文本。
**烧首次下载 ~120MB**（之后离线）+ 真 model embed cpu ~30s 总耗时。

跑：
    python scripts/dogfood_kb.py
    # 或干净沙盒：
    HARNESS_FRAMEWORK_HOME=/tmp/dogfood-$(date +%s) python scripts/dogfood_kb.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── 模拟数据 ─────────────────────────────────────────────────────────────
# 故意造"字面不同语义同"的 claim 对，看真 model 命中

CLAIM_BATCHES = [
    # batch 1 — 同一个发现的不同表述（应触发 dedup merge）
    {
        "label": "batch1: GAP MAE 在 QM9 OOD 上偏高（3 个不同表述）",
        "claims": [
            {
                "claim_text": "GAP MAE > 100 meV/atom on QM9 OOD split",
                "claim_type": "empirical",
                "concept_ids": [], "orphan_reason": "dogfood stub",
                "scope": "project",
                "sources": ["doi:10.1000/paperA"],
                "scope_dimensions": {"dataset": "QM9", "regime": "OOD",
                                       "metric": "MAE"},
                "confidence": 0.7,
            },
            {
                # 字面差异大，语义同
                "claim_text": "GAP method shows large mean absolute error on out-of-distribution QM9 examples",
                "claim_type": "empirical",
                "concept_ids": [], "orphan_reason": "dogfood stub",
                "scope": "project",
                "sources": ["doi:10.1000/paperB"],  # 独立 source
                "scope_dimensions": {"dataset": "QM9", "regime": "OOD",
                                       "metric": "MAE"},
                "confidence": 0.7,
            },
            {
                # 中文版同一意思
                "claim_text": "GAP 方法在 QM9 数据集 OOD 切分下 MAE 高于 100",
                "claim_type": "empirical",
                "concept_ids": [], "orphan_reason": "dogfood stub",
                "scope": "project",
                "sources": ["doi:10.1000/paperC"],
                "scope_dimensions": {"dataset": "QM9", "regime": "OOD"},
                "confidence": 0.7,
            },
        ],
    },
    # batch 2 — 同字面但 scope 真不同（不应 merge —— 验证 scope 区分）
    {
        "label": "batch2: 同字面但 dataset 不同（应当成不同 claim）",
        "claims": [
            {
                "claim_text": "Model X performs poorly on validation set",
                "claim_type": "empirical",
                "concept_ids": [], "orphan_reason": "stub",
                "scope": "project",
                "sources": ["doi:10.2000/expA"],
                "scope_dimensions": {"dataset": "MD17", "regime": "in-distribution"},
                "confidence": 0.6,
            },
            {
                "claim_text": "Model X performs poorly on validation set",
                "claim_type": "empirical",
                "concept_ids": [], "orphan_reason": "stub",
                "scope": "project",
                "sources": ["doi:10.2000/expB"],
                "scope_dimensions": {"dataset": "RMD17", "regime": "OOD"},
                "confidence": 0.6,
            },
        ],
    },
    # batch 3 — 远距离主题不同的 claim（既不该 merge 也不该 warn）
    {
        "label": "batch3: 完全不同主题",
        "claims": [
            {
                "claim_text": "Transformer scales O(n^2) with context length",
                "claim_type": "empirical",
                "concept_ids": [], "orphan_reason": "stub",
                # org 没有出生通道了（两层重构 P1）—— 直写 org 会被守卫拒。
                # 这个脚本测的是语义去重/注入排序，与 scope 无关，落 project 即可。
                "scope": "project",
                "sources": ["doi:10.3000/t1", "doi:10.3000/t2"],
                "confidence": 0.85,
            },
            {
                "claim_text": "Coffee makes me more productive in the morning",
                "claim_type": "empirical",
                "concept_ids": [], "orphan_reason": "stub",
                "scope": "project",
                "sources": ["doi:10.4000/coffee"],
                "confidence": 0.5,
            },
        ],
    },
    # batch 4 — dead_end 触发 dreaming pending
    {
        "label": "batch4: dead_end claim → 立即触发 dreaming pending",
        "claims": [
            {
                "claim_text": "Pure deep ANN without inductive bias fails on small molecule property prediction",
                "claim_type": "dead_end",
                "dont_repeat_reason": "data efficiency issue; converges only with > 1M samples",
                "concept_ids": [], "orphan_reason": "stub",
                "scope": "project",
                "sources": ["doi:10.5000/failed"],
                "confidence": 0.8,
            },
        ],
    },
]


SAMPLE_ARTIFACT_WITH_DISAGREEMENT = """\
# Analysis Report (dogfood test)

The current run shows GAP performs well on QM9 OOD with the new training schedule.

However, looking at the KB:
- I disagree with {claim_id_to_disagree} because our scope uses a different
  fine-tuning protocol (LR=1e-4, 200 epochs), not the one assumed in the original
  claim's experiment.
- The claim should be revisited with our setup parameters.

Conclusion: extend run to 500 epochs and re-check.
"""


# ── Pretty print helpers ────────────────────────────────────────────────────

def section(title: str):
    print()
    print("═" * 72)
    print(f" {title}")
    print("═" * 72)


def kv(k: str, v) -> str:
    return f"  {k:<32}  {v}"


# ── 主流程 ──────────────────────────────────────────────────────────────────

async def main() -> int:
    section("v0.3.2 KB 复利 e2e dogfood (真 embedding model + mock LLM)")

    # 独立 sandbox 防污染本机
    sandbox = Path(tempfile.mkdtemp(prefix="dogfood-kb-v3.2-"))
    os.environ["HARNESS_FRAMEWORK_HOME"] = str(sandbox)
    os.environ["HARNESS_FRAMEWORK_ORG_HOME"] = str(sandbox / "org")
    # 确保 semantic dedup 真启用
    os.environ.pop("HARNESS_DISABLE_SEMANTIC_DEDUP", None)
    print(kv("sandbox", sandbox))
    print(kv("embedding model", os.getenv("HARNESS_EMBEDDING_MODEL",
                                            "intfloat/multilingual-e5-small")))

    from core.bootstrap import bootstrap
    from core.state import State
    bootstrap()

    state = State.new(
        node_type="literature",
        base_dir=sandbox / "runs",
        project_id="dogfood_proj",
    )
    print(kv("project_id", "dogfood_proj"))
    print(kv("run_id", state.run_id))

    # ── Batch 1: 真 embedding 对同义不同字面的 claim 应触发 merge ───────────
    section("Batch 1: 写 3 条字面不同语义同的 claim")
    written: list[dict] = []
    for i, rec in enumerate(CLAIM_BATCHES[0]["claims"], 1):
        t0 = time.time()
        final, created = state.write_kb("claims", dict(rec))
        dt = time.time() - t0
        action = "CREATED" if created else "MERGED"
        merged_into = "" if created else f" → into {final['id']}"
        dedup = (final.get("derived") or {}).get("dedup_info") or {}
        cos = dedup.get("cosine")
        print(f"  [{i}] {action}{merged_into}  ({dt*1000:.0f}ms)")
        print(f"      text: {rec['claim_text'][:70]}")
        if cos is not None:
            print(f"      cosine to nearest: {cos:.3f}")
        if not created:
            print(f"      indep_source_count: {final.get('independent_source_count')}")
            print(f"      replication_count: {final.get('replication_count')}")
            print(f"      sources: {final.get('sources')}")
        written.append(final)

    # 期望：3 条全 merge 到 same id（因为同义），indep_source_count=3
    distinct_ids = {f["id"] for f in written}
    print()
    if len(distinct_ids) == 1:
        print(f"  ✅ 3 条 claim 真合并成 1 个 ({list(distinct_ids)[0]})")
        final = state.get_kb_record("claims", list(distinct_ids)[0])
        if final:
            print(f"     indep_source_count={final.get('independent_source_count')}")
            print(f"     replication_count={final.get('replication_count')}")
    elif len(distinct_ids) <= 2:
        print(f"  ⚠️  3 条 claim 被分成 {len(distinct_ids)} 个；merge 部分成功")
    else:
        print(f"  ❌ 3 条 claim 没合并（cosine 太低 / template 没对齐）")

    # ── Batch 2: 同字面但 scope 不同 → 应当成不同 claim ───────────────────
    section("Batch 2: 同字面但 scope_dimensions 不同（应区分）")
    for i, rec in enumerate(CLAIM_BATCHES[1]["claims"], 1):
        final, created = state.write_kb("claims", dict(rec))
        action = "CREATED" if created else "MERGED"
        print(f"  [{i}] {action} {final['id']}")
        print(f"      scope: {rec['scope_dimensions']}")

    # ── Batch 3: 远距离主题 ───────────────────────────────────────────────
    section("Batch 3: 不相关主题（确认远距 cosine 不触发误 merge）")
    for i, rec in enumerate(CLAIM_BATCHES[2]["claims"], 1):
        try:
            final, created = state.write_kb("claims", dict(rec))
            print(f"  [{i}] CREATED {final['id']}  ({rec['claim_type']})")
        except Exception as e:
            print(f"  [{i}] ❌ {type(e).__name__}: {str(e)[:120]}")

    # ── Batch 4: dead_end → dreaming pending 立即触发 ─────────────────────
    section("Batch 4: dead_end claim → 检查 dreaming auto-trigger")
    for rec in CLAIM_BATCHES[3]["claims"]:
        final, created = state.write_kb("claims", dict(rec))
        print(kv("dead_end claim", final["id"]))

    from core.dreaming_scheduler import read_pending, _read_counter
    pending = read_pending("dogfood_proj")
    counter = _read_counter("dogfood_proj")
    print(kv("dreaming_pending", "yes" if pending else "no"))
    if pending:
        print(kv("pending.reasons", pending.get("reasons")))
    print(kv("kb_writes_since_dreaming", counter))

    # ── Context engine 真 semantic 注入测试 ────────────────────────────────
    section("Context engine: 真 semantic 检索注入")
    from core.context_engine import build_messages, _search_kb_for_context
    from core.loader import load_harness
    # 这个节点在抽象架构里就是 **Analysis**（harness.yaml 自述"你是 Analysis"，
    # v0.5 起一等公民是研究问题）。目录名与 node_type 仍是 hypothesis，
    # 而 load_harness 按 node_type 寻址 —— 所以这里必须写 hypothesis。
    h = load_harness("hypothesis")
    state2 = State.new(
        node_type="hypothesis",
        base_dir=sandbox / "runs",
        project_id="dogfood_proj",
    )
    t0 = time.time()
    msgs = build_messages(h, state2, {"question": "GAP performance on QM9"})
    dt = time.time() - t0
    print(kv("build_messages time", f"{dt*1000:.0f}ms (含 embed query)"))
    user_msg = msgs[1].content or ""
    # 看注入了哪些 claim
    print()
    print("  [user prompt 头 1500 字符]")
    print()
    for ln in user_msg.split("\n")[:30]:
        print(f"    {ln[:120]}")
    print()
    # 验证启发式 rule + caveat 出现
    sys_msg = msgs[0].content or ""
    sig = []
    sig.append(("启发式 rule (KB 启发式)", "KB 使用启发式" in sys_msg))
    sig.append(("启发式 rule (disagree pattern)", "I disagree with claim_" in sys_msg))
    sig.append(("user prompt 含 '历史记录参考'", "历史记录参考" in user_msg or "ground truth" in user_msg))
    sig.append(("dead_end caveat (仅在 scope 成立)", "仅在" in user_msg and "scope" in user_msg.lower()))
    for label, ok in sig:
        print(kv(label, "✓" if ok else "✗"))

    # ── Curator scan_artifact_disagreements ──────────────────────────────
    section("Curator scan_artifact_disagreements 真扫")
    # 写一个 artifact 含 disagreement，用第一条 written claim 的 id
    target_id = written[0]["id"]
    art_content = SAMPLE_ARTIFACT_WITH_DISAGREEMENT.format(
        claim_id_to_disagree=target_id,
    )
    art = state2.save_artifact(
        "analysis_report", "dogfood_test", art_content,
    )
    from core.tool_registry import execute as execute_tool
    res = await execute_tool(
        "scan_artifact_disagreements", state2,
        artifact_ids=[art["id"]], auto_propose=True,
    )
    print(kv("scan status", res.get("status")))
    print(kv("findings", res.get("n_findings")))
    print(kv("proposals_created", res.get("proposals_created")))
    if res.get("found"):
        f0 = res["found"][0]
        print(kv("first finding claim_id", f0.get("claim_id")))
        print(kv("first finding label", f0.get("label")))
        print(kv("first finding reason", (f0.get("reason") or "")[:80]))

    # ── stale dead_end 找寻 ───────────────────────────────────────────────
    section("find_stale_dead_end_or_refuted (days=0 全找)")
    res = await execute_tool(
        "find_stale_dead_end_or_refuted", state2, days=0, limit=10,
    )
    print(kv("found stale", res.get("count", 0)))
    for s in res.get("stale_claims", [])[:3]:
        print(f"    - {s['claim_id']} ({s['claim_type']}): "
              f"{s['claim_text'][:60]}")

    # ── 索引状态 ─────────────────────────────────────────────────────────
    section("Vector index status")
    from core.kb_vector_index import load_index, read_manifest
    for scope in ("org", "project"):
        proj = "dogfood_proj" if scope == "project" else None
        for ent in ("claims", "concepts"):
            try:
                vecs, ids = load_index(ent, scope, proj)
                print(kv(f"{scope}/{ent}", f"{vecs.shape[0]} vectors"))
            except Exception as e:
                print(kv(f"{scope}/{ent}", f"err: {e}"))
        m = read_manifest(scope, proj)
        if m:
            print(kv(f"{scope} manifest model", m.get("model_id")))

    # ── 总结 ─────────────────────────────────────────────────────────────
    section("Dogfood Summary")
    all_claims = state.list_kb("claims")
    print(kv("total claims in KB", len(all_claims)))
    print(kv("sandbox kept at", sandbox))
    print()
    print("看完不需要 → rm -rf", sandbox)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
