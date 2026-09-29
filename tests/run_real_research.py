"""真课题端到端验证：跑 orchestrator → literature → curator 完整链路。

主题：MLIP foundation model 分布偏移鲁棒性（user 的真实研究方向）。

观察点：
  - orchestrator 收到研究请求 → 正确判断 4 类意图 → 调 run_node(literature)？
  - literature 节点 → 真调 paper search → 写 survey_report artifact？
  - 子节点完成 → orchestrator 看到 pending_curator_integrations 提醒 → 调 curator？
  - curator 整合 → 写入 KB（claim/concept）？
  - 整个链路的 token 消耗、turn 数、产出真实性

运行方式：
  python tests/run_real_research.py
  python tests/run_real_research.py --skip-curator  # 跳过 curator 阶段（更便宜）

输出：
  output/orchestrator__validation_mlip_<ts>/ ↑ 完整 state（含 transcript + conversation）
  跑完后 dump：artifacts / KB / memory / 各阶段 turn 数 / token 消耗
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from core.agent_loop import resume_loop, run_loop  # noqa: E402
from core.bootstrap import bootstrap  # noqa: E402
from core.context_engine import build_messages  # noqa: E402
from core.llm import LLMClient, LLMMessage  # noqa: E402
from core.loader import load_harness  # noqa: E402
from core.pause import clear_all, get_paused_run  # noqa: E402
from core.state import State  # noqa: E402


def _make_state(project_id: str, base_dir: Path) -> State:
    """跟 chat.py 一样的 state 构造方式，保证持久化兼容。"""
    from core.state import _project_root
    run_id = f"orchestrator__{project_id}"
    root = base_dir / run_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)
    project_root = _project_root(project_id)
    return State(
        run_id=run_id, node_type="_orchestrator",
        root=root, project_id=project_id, project_root=project_root,
    )


def _dump_summary(state: State, label: str):
    print(f"\n{'=' * 60}")
    print(f"📊 {label}")
    print("=" * 60)
    _b = "unlimited" if state.tokens_limit <= 0 else f"{state.tokens_limit:,}"
    print(f"  tokens_used: {state.tokens_used:,} / {_b}")
    print(f"  tool_calls_made: {state.tool_calls_made}")

    arts = state.list_artifacts()
    print(f"  artifacts: {len(arts)}")
    for a in arts[:20]:
        print(f"    - {a['id']} (type={a['type']})")

    print(f"  memory rows: {len(state.list_memory())}")
    for m in state.list_memory()[:10]:
        kind = m.get("kind", "?")
        text = (m.get("text") or "")[:80]
        print(f"    - [{kind}] {text}")

    kb_counts = {}
    for ent in ("concepts", "claims", "experiments", "chunks"):
        kb_counts[ent] = len(state.list_kb(ent))
    print(f"  KB: {kb_counts}")
    if kb_counts["claims"]:
        from collections import Counter
        types = Counter(r.get("claim_type", "?")
                          for r in state.list_kb("claims"))
        print(f"  KB.claims by type: {dict(types)}")

    pending = state.hook_state.get("pending_curator_integrations") or []
    if pending:
        print(f"  ⚠️ pending curator integrations: {len(pending)}")
        for p in pending:
            print(f"    - producing={p['producing_node']}, artifacts={p.get('imported_artifact_ids', [])}")


async def _run_one_user_turn(state, harness, messages, llm, user_text: str,
                              label: str):
    """跑一次 user → orchestrator → tools loop，处理 pause（自动答）。"""
    print(f"\n{'─' * 60}")
    print(f"▶️  USER ({label}):")
    print(f"   {user_text[:200]}")
    print("─" * 60)

    messages.append(LLMMessage(role="user", content=user_text))
    t0 = time.time()
    result = await run_loop(harness, state, messages, llm)
    dt = time.time() - t0

    # pause handling: 自动回答（避免 stdin 阻塞）
    while result.status == "paused":
        pe = result.pause_event
        print(f"\n⏸ orchestrator 触发 pause: {pe.question[:120]}")
        auto_answer = "你来判断最合理的下一步，按你的判断推进。"
        print(f"   自动答：{auto_answer}")
        ctx = get_paused_run(state.run_id)
        if ctx is None:
            print("   ❌ pause ctx 丢失")
            break
        result = await resume_loop(ctx, auto_answer)

    print(f"\n💬 ORCHESTRATOR ({result.status}, {result.turns} turns, {dt:.1f}s):")
    text = result.final_text or "(空)"
    print(text[:1500] + ("\n... [truncated]" if len(text) > 1500 else ""))
    return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-curator", action="store_true",
                        help="跳过手动触发 curator 的第二轮（更便宜）")
    parser.add_argument("--project-id", default=None,
                        help="项目 id（默认 validation_mlip_<ts>）")
    args = parser.parse_args()

    if not os.getenv("LLM_API_KEY"):
        print("❌ LLM_API_KEY 未设置。在 .env 填 LLM_API_KEY + LLM_BASE_URL + LLM_MODEL，详见 .env.example")
        return 1

    project_id = args.project_id or f"validation_mlip_{int(time.time())}"
    base_dir = Path(os.getenv("STATE_DIR", "./output"))
    base_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 真课题验证：MLIP foundation model robustness ===")
    print(f"project_id: {project_id}")
    print(f"state dir : {base_dir}/orchestrator__{project_id}")

    bootstrap()
    clear_all()
    state = _make_state(project_id, base_dir)
    harness = load_harness("_orchestrator")
    llm = LLMClient()
    messages = build_messages(harness, state, node_inputs={})

    _dump_summary(state, "起始状态")

    # ── Turn 1：研究请求 ────────────────────────────────────────────────
    await _run_one_user_turn(
        state, harness, messages, llm,
        user_text=(
            "我在做 foundation MLIP（machine learning interatomic potential）"
            "的分布偏移鲁棒性研究。请帮我做一份简短文献调研，"
            "重点找：(1) MLIP foundation model（MACE / Orb / SevenNet 等）的 OOD 评估方法 "
            "(2) 已观察到的分布偏移失败模式 (3) 提升鲁棒性的现有方法。"
            "5-10 篇代表性论文即可，不需要穷举。"
        ),
        label="提研究需求",
    )

    _dump_summary(state, "Turn 1 完成（应该已起 literature + 可能起 curator）")

    if args.skip_curator:
        print("\n(--skip-curator 模式，停在第一轮)")
        return 0

    # ── Turn 2：让 orchestrator 看 KB 状态 + 推荐下一步 ────────────────────
    await _run_one_user_turn(
        state, harness, messages, llm,
        user_text=(
            "看一下 KB 现在沉淀了什么。然后告诉我：根据这次调研，"
            "如果要做一个验证 foundation MLIP 在分布外失败的实验，"
            "你建议最值得测试的假设是什么？2 个就够。"
        ),
        label="问 KB 状态 + 要假设建议",
    )

    _dump_summary(state, "Turn 2 完成（最终状态）")

    print(f"\n{'=' * 60}")
    print(f"✅ 跑完。state 在：{state.root}")
    print(f"   conversation: {state.root}/conversation.json")
    print(f"   transcript  : {state.root}/transcript.jsonl")
    print(f"   project KB  : {state.project_root}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
