"""完整 pipeline 验证：LJ 流体平衡 → MD 模拟。

链路：orchestrator → literature → hypothesis → data → experiment(LAMMPS)
       → postprocess → analysis → writing → review → curator

主题：Lennard-Jones 流体在 T=1.0, ρ=0.85 下 1000 步内达到稳态平衡？
（最简单的 LAMMPS demo，非常稳，方便 smoke 验证整个 pipeline。）

观察：
  - orchestrator 调度顺序正确吗？
  - 每个节点的输入/输出契约执行了吗？
  - artifact 跨节点正确传递了吗？
  - curator 在 producing 节点之后真被调起了吗？
  - LAMMPS 真的跑起来了吗（lmp_serial 真调用，有真 log 输出）？
  - 最终 manuscript + review 是否合理？

成本预估：~500 万 token，¥5-10，10-20 分钟。
"""
from __future__ import annotations

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
from core.state import State, _project_root  # noqa: E402


def _make_state(project_id: str, base_dir: Path) -> State:
    run_id = f"orchestrator__{project_id}"
    root = base_dir / run_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)
    project_root = _project_root(project_id)
    return State(
        run_id=run_id, node_type="_orchestrator",
        root=root, project_id=project_id, project_root=project_root,
    )


def _dump_state(state: State, label: str):
    print(f"\n{'━' * 70}")
    print(f"📊 {label}")
    print("━" * 70)
    _b = "unlimited" if state.tokens_limit <= 0 else f"{state.tokens_limit:,}"
    print(f"  tokens_used: {state.tokens_used:,} / {_b}")
    print(f"  tool_calls : {state.tool_calls_made}")
    arts = state.list_artifacts()
    print(f"  artifacts  : {len(arts)}")
    for a in arts[:30]:
        print(f"    - {a['id']:<50}  type={a['type']}")
    kb_counts = {ent: len(state.list_kb(ent))
                 for ent in ("concepts", "claims", "experiments", "chunks")}
    nonzero = {k: v for k, v in kb_counts.items() if v > 0}
    if nonzero:
        print(f"  KB         : {nonzero}")
    if kb_counts.get("claims"):
        from collections import Counter
        claim_types = Counter(r.get("claim_type", "?")
                                for r in state.list_kb("claims"))
        print(f"  KB.claims by type: {dict(claim_types)}")
    mem = state.list_memory()
    if mem:
        print(f"  memory     : {len(mem)} 条")
    pending = state.hook_state.get("pending_curator_integrations") or []
    if pending:
        print(f"  ⚠️ pending curator: {len(pending)}")


async def _drive(state, harness, messages, llm, user_text: str, label: str):
    """跑一次 user turn。自动答 pause（不阻塞 stdin）。"""
    print(f"\n{'─' * 70}")
    print(f"▶️  USER ({label}):")
    print(f"   {user_text[:300]}")
    print("─" * 70)

    messages.append(LLMMessage(role="user", content=user_text))
    t0 = time.time()
    result = await run_loop(harness, state, messages, llm)
    while result.status == "paused":
        pe = result.pause_event
        print(f"\n⏸ pause: {pe.question[:150]}")
        ans = "按你的判断继续推进。"
        print(f"   自动答：{ans}")
        ctx = get_paused_run(state.run_id)
        if ctx is None:
            print("   ❌ ctx 丢失")
            break
        result = await resume_loop(ctx, ans)
    dt = time.time() - t0

    print(f"\n💬 ORCHESTRATOR ({result.status}, {result.turns} turns, {dt:.1f}s):")
    text = result.final_text or "(空)"
    print(text[:2000] + ("\n... [truncated]" if len(text) > 2000 else ""))
    return result


async def main() -> int:
    if not os.getenv("LLM_API_KEY"):
        print("❌ LLM_API_KEY 未设置。在 .env 填 LLM_API_KEY + LLM_BASE_URL + LLM_MODEL，详见 .env.example")
        return 1
    if not Path("/opt/homebrew/bin/lmp_serial").exists():
        print("❌ /opt/homebrew/bin/lmp_serial 不存在")
        return 1

    pid = f"lammps_smoke_{int(time.time())}"
    base = Path(os.getenv("STATE_DIR", "./output"))
    base.mkdir(parents=True, exist_ok=True)

    print(f"=== LAMMPS Pipeline Smoke ===")
    print(f"project_id: {pid}")
    print(f"state dir : {base}/orchestrator__{pid}")
    print(f"lmp binary: /opt/homebrew/bin/lmp_serial")

    bootstrap()
    clear_all()
    state = _make_state(pid, base)
    harness = load_harness("_orchestrator")
    llm = LLMClient()
    messages = build_messages(harness, state, node_inputs={})

    _dump_state(state, "起始")

    # 一句话研究请求，让 orchestrator 自己规划整个 pipeline
    await _drive(
        state, harness, messages, llm,
        user_text=(
            "我要做一个简单的小研究项目验证 pipeline。"
            "主题：Lennard-Jones 流体在 T=1.0、ρ=0.85（reduced units）下，"
            "用 LAMMPS 跑 1000 步 NVT MD，验证假设：温度在 500 步内达到稳态"
            "（500 步之后温度漂移 < 5%）。\n\n"
            "请按 **完整 pipeline 流程** 推进：\n"
            "1. literature 节点：简单查一下 LJ MD 文献最佳实践（限 3-5 篇足够）\n"
            "2. hypothesis 节点：写一个可证伪 prereg（含 falsification_criteria）\n"
            "3. data 节点：用 LAMMPS 内置 lattice 命令生成 fcc 初始结构（不用外部文件）\n"
            "4. experiment 节点：跑 lmp_serial（binary 在 /opt/homebrew/bin/lmp_serial）\n"
            "5. postprocess 节点：解析 log 提取每 50 步的温度\n"
            "6. analysis 节点：对账 prereg，判 hypothesis verdict\n"
            "7. writing 节点：写一段短 manuscript\n"
            "8. review 节点：单 persona 评审\n\n"
            "每个 producing 节点结束记得调 _curator 整合。"
            "目标：跑通整个 pipeline，验证框架。**没有 token / turn 限制**，"
            "按你的判断认真做。"
        ),
        label="完整 pipeline 请求",
    )

    _dump_state(state, "最终状态")

    print(f"\n{'=' * 70}")
    print(f"✅ pipeline 跑完")
    print(f"   conversation: {state.root}/conversation.json")
    print(f"   transcript  : {state.root}/transcript.jsonl")
    print(f"   tokens      : {state.tokens_used:,}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
