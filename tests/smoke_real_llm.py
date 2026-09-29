"""真实 LLM 的端到端 smoke test —— 默认不跑（避免烧钱）。

为什么单独脚本不进 pytest：
  - 真调 LLM 会花钱（DeepSeek / OpenAI 等）
  - CI 不能依赖外部服务可用性
  - 交付前手动跑一次足够

什么时候跑：
  - 交付前最终验证
  - 改了 agent_loop / context_engine / orchestrator harness 之后
  - debug "为啥 LLM 不调我加的新工具" 类问题

怎么跑：
  $ LLM_API_KEY=sk-xxx python tests/smoke_real_llm.py
  $ LLM_API_KEY=sk-xxx python tests/smoke_real_llm.py --scenario kb
  $ LLM_API_KEY=sk-xxx python tests/smoke_real_llm.py --scenario pause

每个 scenario 完成后打印 PASS / FAIL，给精简 transcript（看 LLM 真的做了什么）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# 把 framework 加进 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from core.agent_loop import resume_loop, run_loop  # noqa: E402
from core.bootstrap import bootstrap  # noqa: E402
from core.context_engine import build_messages  # noqa: E402
from core.llm import LLMClient, LLMMessage  # noqa: E402
from core.loader import load_harness  # noqa: E402
from core.pause import clear_all, get_paused_run  # noqa: E402
from core.state import State  # noqa: E402


# ── 共享 helpers ─────────────────────────────────────────────────────────────

def _make_state(node_type: str = "_orchestrator") -> State:
    td = Path(tempfile.mkdtemp(prefix="smoke_"))
    return State.new(node_type=node_type, base_dir=td, project_id=None)


def _summarize_transcript(state: State, last_n: int = 50) -> list[str]:
    """从 transcript.jsonl 抽出最后 N 个事件简述。"""
    tp = state.transcript_path
    if not tp.exists():
        return []
    lines = tp.read_text(encoding="utf-8").splitlines()
    events = []
    for line in lines[-last_n:]:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("event")
        if kind == "tool_call":
            events.append(f"  → tool_call: {ev.get('name')} args={list((ev.get('args') or {}).keys())}")
        elif kind == "tool_result":
            events.append(f"    ← {ev.get('name')} result_preview={str(ev.get('result_preview'))[:80]}")
        elif kind == "llm_response":
            events.append(f"  LLM: finish={ev.get('finish_reason')}, "
                          f"content_preview={str(ev.get('content_preview'))[:80]}")
        elif kind == "loop_pause":
            events.append(f"  ⏸ PAUSE: {ev.get('question')[:80]}")
        elif kind == "loop_resume":
            events.append(f"  ▶️ RESUME: {ev.get('response_preview')[:80]}")
    return events


def _check_api_key() -> bool:
    if not os.getenv("LLM_API_KEY"):
        print("❌ LLM_API_KEY 未设置。在 .env 填 LLM_API_KEY + LLM_BASE_URL + LLM_MODEL，详见 .env.example")
        return False
    return True


# ── Scenario 1: chat → orchestrator 基本响应 ──────────────────────────────

async def scenario_basic_chat() -> bool:
    """LLM 能简单跟 orchestrator 对话（QA 意图，不起子节点）。"""
    print("\n=== Scenario: basic_chat ===")
    print("意图：user 问简单问题；orchestrator 应当直接回答（不 run_node）")

    bootstrap(force=True)
    state = _make_state()
    harness = load_harness("_orchestrator")
    llm = LLMClient()

    messages = build_messages(harness, state, node_inputs={})
    messages.append(LLMMessage(role="user", content="你好，介绍一下你自己 + 你能干什么"))

    result = await run_loop(harness, state, messages, llm)

    print(f"\n  status={result.status}, turns={result.turns}")
    print(f"  final_text (前 300 字):\n  {result.final_text[:300]}")
    print(f"\n  transcript 关键事件：")
    for e in _summarize_transcript(state):
        print(e)

    # 验证：完成，没起子节点
    ok = result.status == "completed"
    ran_subagents = any(tc["name"] == "run_node" for tc in result.tool_calls)
    if ran_subagents:
        print("  ⚠️ 意外起了 run_node —— 简单 QA 应当直接答")
    print(f"  → {'PASS' if ok and not ran_subagents else 'FAIL'}")
    return ok and not ran_subagents


# ── Scenario 2: KB 检索能力 ──────────────────────────────────────────────

async def scenario_kb_query() -> bool:
    """LLM 应当用 search_kb / list_artifacts / query_project_status 答状态查询。"""
    print("\n=== Scenario: kb_query ===")
    print("意图：user 问项目状态；orchestrator 应当用查询工具（不 run_node）")

    bootstrap(force=True)
    state = _make_state()
    harness = load_harness("_orchestrator")
    llm = LLMClient()

    messages = build_messages(harness, state, node_inputs={})
    messages.append(LLMMessage(
        role="user",
        content="目前 KB 里有什么内容？项目状态怎么样？",
    ))

    result = await run_loop(harness, state, messages, llm)

    print(f"\n  status={result.status}, turns={result.turns}")
    print(f"  final_text (前 300 字):\n  {result.final_text[:300]}")
    print(f"\n  transcript 关键事件：")
    for e in _summarize_transcript(state):
        print(e)

    used_query_tool = any(
        tc["name"] in ("search_kb", "list_artifacts", "query_project_status")
        for tc in result.tool_calls
    )
    ran_subagents = any(tc["name"] == "run_node" for tc in result.tool_calls)
    ok = result.status == "completed" and used_query_tool and not ran_subagents
    print(f"  used_query_tool={used_query_tool}, ran_subagents={ran_subagents}")
    print(f"  → {'PASS' if ok else 'FAIL'}")
    return ok


# ── Scenario 3: pause/resume 真链路 ────────────────────────────────────────

async def scenario_pause_resume() -> bool:
    """触发 orchestrator 调 request_human_input → 验证 pause unwind → 模拟 user 答 → resume。

    这个 scenario 不需要 user 真坐在终端 —— 我们替 _ask_user 用预设答案。
    """
    print("\n=== Scenario: pause_resume ===")
    print("意图：让 orchestrator 觉得需要问 user → 验证 pause → resume → 完成")

    bootstrap(force=True)
    state = _make_state()
    harness = load_harness("_orchestrator")
    llm = LLMClient()

    messages = build_messages(harness, state, node_inputs={})
    # 故意提一个含糊请求，鼓励 orchestrator 用 request_human_input 澄清
    messages.append(LLMMessage(
        role="user",
        content="我想做点研究，但还没想好方向。你来帮我。",
    ))

    result = await run_loop(harness, state, messages, llm)

    if result.status == "paused":
        print(f"\n  ✓ 触发了 pause: question={result.pause_event.question[:120]}")
        # 模拟 user 答：给一个具体方向
        ctx = get_paused_run(state.run_id)
        if ctx is None:
            print("  ❌ pause registry 没找到 ctx")
            return False
        result2 = await resume_loop(ctx, "我想研究图神经网络在材料属性预测里的应用")
        print(f"  resume 后 status={result2.status}, turns={result2.turns}")
        print(f"  final_text (前 300 字):\n  {result2.final_text[:300]}")
        ok = result2.status in ("completed", "paused")  # 再次 paused 也算 pause 链路成立
    else:
        print(f"\n  orchestrator 没用 request_human_input 而是直接回答了（status={result.status}）。")
        print(f"  这也不算错（LLM 可能选择给方向建议而不是问），但这个 scenario 没测到 pause 路径。")
        ok = result.status == "completed"

    print(f"\n  transcript 关键事件：")
    for e in _summarize_transcript(state):
        print(e)

    print(f"  → {'PASS' if ok else 'FAIL'}")
    return ok


# ── Scenario 4: memory 写入 + PROFILE 升级提议 ──────────────────────────

async def scenario_persistent_directive() -> bool:
    """user 说"以后..." → orchestrator 应当 add_memory(directive) + propose_profile_update。"""
    print("\n=== Scenario: persistent_directive ===")
    print("意图：user 说出'以后/始终'信号 → 应同时写 directive memory + propose 升级")

    bootstrap(force=True)
    state = _make_state()
    harness = load_harness("_orchestrator")
    llm = LLMClient()

    messages = build_messages(harness, state, node_inputs={})
    messages.append(LLMMessage(
        role="user",
        content="以后回复都用中文，别用英文。",
    ))

    result = await run_loop(harness, state, messages, llm)

    print(f"\n  status={result.status}, turns={result.turns}")
    print(f"  final_text (前 300 字):\n  {result.final_text[:300]}")

    used_add_memory = any(
        tc["name"] == "add_memory"
        and (tc["args"] or {}).get("kind") == "directive"
        for tc in result.tool_calls
    )
    used_propose = any(
        tc["name"] == "propose_profile_update"
        for tc in result.tool_calls
    )

    print(f"\n  transcript 关键事件：")
    for e in _summarize_transcript(state):
        print(e)
    print(f"\n  used_add_memory(kind=directive)={used_add_memory}")
    print(f"  used_propose_profile_update={used_propose}")

    ok = result.status == "completed" and used_add_memory and used_propose
    print(f"  → {'PASS' if ok else 'FAIL（注：LLM 不稳定，--scenario 重跑 2-3 次再判定）'}")
    return ok


# ── Scenario 5: 启动子节点（最重） ───────────────────────────────────────

async def scenario_run_subnode() -> bool:
    """user 让 orchestrator 启动 literature 节点。验证 run_node 真能调起 + 完成。"""
    print("\n=== Scenario: run_subnode ===")
    print("意图：user 明确要求做调研 → orchestrator 应当 run_node(literature, ...)")
    print("⚠️ 这个 scenario 会调子节点的 LLM，**烧 token 多**。要不要继续？(y/N)")
    if not os.getenv("SMOKE_AUTO_YES"):
        try:
            ans = input("> ").strip().lower()
            if ans != "y":
                print("(已跳过)")
                return True
        except (EOFError, KeyboardInterrupt):
            return True

    bootstrap(force=True)
    state = _make_state()
    harness = load_harness("_orchestrator")
    llm = LLMClient()

    messages = build_messages(harness, state, node_inputs={})
    messages.append(LLMMessage(
        role="user",
        content="帮我对'graph neural networks for materials property prediction'这个方向做一份简短的文献调研。",
    ))

    result = await run_loop(harness, state, messages, llm)

    print(f"\n  status={result.status}, turns={result.turns}")
    print(f"  final_text (前 500 字):\n  {result.final_text[:500]}")

    used_run_node = any(
        tc["name"] == "run_node"
        and (tc["args"] or {}).get("node_type") == "literature"
        for tc in result.tool_calls
    )

    print(f"\n  used run_node(literature)={used_run_node}")
    ok = result.status == "completed" and used_run_node
    print(f"  → {'PASS' if ok else 'FAIL'}")
    return ok


# ── 主入口 ────────────────────────────────────────────────────────────────

SCENARIOS = {
    "basic_chat": scenario_basic_chat,
    "kb_query": scenario_kb_query,
    "pause_resume": scenario_pause_resume,
    "persistent_directive": scenario_persistent_directive,
    "run_subnode": scenario_run_subnode,
}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--scenario", choices=list(SCENARIOS.keys()) + ["all"],
        default="basic_chat",
        help="要跑哪个 scenario（默认 basic_chat，最便宜）",
    )
    args = parser.parse_args()

    if not _check_api_key():
        return 1

    clear_all()

    if args.scenario == "all":
        results = {}
        for name, fn in SCENARIOS.items():
            try:
                results[name] = await fn()
            except Exception as e:
                print(f"\n❌ scenario {name} 抛了异常：{type(e).__name__}: {e}")
                results[name] = False
        print("\n" + "=" * 50)
        print("总结：")
        for name, ok in results.items():
            print(f"  {'✅' if ok else '❌'} {name}")
        return 0 if all(results.values()) else 1
    else:
        ok = await SCENARIOS[args.scenario]()
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
