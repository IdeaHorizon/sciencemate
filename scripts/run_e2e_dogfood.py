"""E2E dogfood driver —— 给 orchestrator 一个 user prompt，auto-approve 所有
pause，让它跑到完成。

v0.7 起支持中途 control（不必 kill）：
  - kill 后 resume：messages 每 turn 自动 checkpoint 到 messages_checkpoint.json，
    `--resume` 直接接着跑（artifact/KB 自然续连，因 project_id 共享）。
  - 中途 inject / pause / abort：另开终端跑
    `python -m core.signal inject <project_id> "your message"` 即可（hook
    每 turn_start 检查 signal file）。

用法：
  # 新跑
  python scripts/run_e2e_dogfood.py "<prompt>" --project <id>

  # kill 后从 checkpoint 续跑
  python scripts/run_e2e_dogfood.py --resume --project <id>

  # 从 checkpoint 续跑 + 给一条新指令
  python scripts/run_e2e_dogfood.py --resume --project <id> \
      --inject "基于现有 artifacts 跳过 literature/hypothesis 直接 analysis"

  # 中途从另一终端 inject (任意时刻可调)
  python -m core.signal inject <project_id> "wrap up now"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from core import pause_driver
from core.agent_loop import run_loop, load_messages_checkpoint
from core.bootstrap import bootstrap
from core.context_engine import build_messages
from core.executor import finalize_run
from core.llm import LLMClient, LLMMessage
from core.loader import load_harness
from core.pause_driver import drive_pause_chain
from core.paths import runs_parent


# conversation.json 唯一读写实现在 core/conversation_store（与 chat.py 共用）。
# 别在这里自带副本——2026-07-08 之前这里写 list、chat.py 写 dict，换入口续连
# 直接崩，且 list 格式把 scratchpad/hook_state 元数据全丢了。
from core.conversation_store import save_conversation as _save_conversation  # noqa: E402


def _make_or_load_state(project_id: str, base_dir: Path):
    """复用 chat.py 同款逻辑：orchestrator state 在 base/orchestrator__<id>/。"""
    from chat import _make_or_load_orchestrator_state
    return _make_or_load_orchestrator_state(project_id, base_dir)


_MAX_AUTO_RESUME_ATTEMPTS = 3


async def run_with_auto_resume(harness, state, messages: list[LLMMessage], llm):
    """跑 run_loop；瞬时网络错自动从 checkpoint resume（v3.2 修复）。

    v8 dogfood 实测：一个 httpx.ConnectError（LLM client 自身 retry 耗尽后仍
    失败）就让整个 2 小时 / $25 的 run 在 experiment 阶段原地崩溃 —— 没人盯着
    的话这条 run 就废了。agent_loop 本来就每 turn 写 messages_checkpoint.json
    （模块 docstring 说的"kill 后 resume"），这里把"重新调 --resume"的动作
    自动化，而不是纯靠人工发现进程崩溃。

    重试耗尽（默认 3 次）后重新抛出，不无限重试掩盖真实的持续性故障。

    返回 (LoopResult, 最终使用的 messages)。
    """
    from core.llm import _RETRYABLE_HTTPX

    attempt = 0
    while True:
        try:
            result = await run_loop(harness, state, messages, llm)
            return result, messages
        except _RETRYABLE_HTTPX as e:
            attempt += 1
            if attempt > _MAX_AUTO_RESUME_ATTEMPTS:
                print(f"\n❌ {type(e).__name__} 重试 {_MAX_AUTO_RESUME_ATTEMPTS} "
                      f"次后仍失败，放弃自动 resume（可手动 --resume 续跑）：{e}")
                raise
            delay = min(5 * (2 ** (attempt - 1)), 60)
            print(f"\n⚠️  瞬时网络错 {type(e).__name__}: {str(e)[:150]}")
            print(f"   自动从 checkpoint resume（第 {attempt}/{_MAX_AUTO_RESUME_ATTEMPTS} "
                  f"次），{delay}s 后重试...")
            state.append_transcript(
                "e2e_dogfood_auto_resume", attempt=attempt,
                error_type=type(e).__name__, error=str(e)[:500],
            )
            await asyncio.sleep(delay)
            cp = load_messages_checkpoint(state)
            if cp is not None:
                messages, _last_turn = cp


async def _amain():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("prompt", nargs="?", default=None,
                        help="给 orchestrator 的研究要求（resume 时可省略）")
    parser.add_argument("--project", required=True,
                        help="项目 id（持久化 memory + KB 必备）")
    parser.add_argument("--max-orchestrator-turns", type=int, default=80,
                        help="orchestrator 单轮 user message 最多跑几轮（保护）")
    parser.add_argument("--resume", action="store_true",
                        help="v0.7：从 messages_checkpoint.json 续跑（kill 后用）")
    parser.add_argument("--inject", default=None,
                        help="v0.7：续跑前先注入一条 user message（如新指令）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.resume and not args.prompt:
        parser.error("非 --resume 模式必须传 prompt")

    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("httpcore", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    bootstrap()
    pause_driver.set_auto_approve(True, countdown_sec=3)

    # v0.8: 项目嵌套 runs。STATE_DIR env var 是 deprecated（backward-compat），
    # 默认走 runs_parent(project_id) → projects/<id>/runs/ 或 runs_anon/。
    if os.getenv("STATE_DIR"):
        base_dir = Path(os.getenv("STATE_DIR"))
    else:
        base_dir = runs_parent(args.project)
    base_dir.mkdir(parents=True, exist_ok=True)

    state = _make_or_load_state(args.project, base_dir)
    harness = load_harness("_orchestrator")
    # max_turns 保护：orchestrator 默认可能 0=无限；e2e dogfood 限上限防失控
    harness.max_turns = args.max_orchestrator_turns
    llm = LLMClient()
    state.hook_state["auto_approve_enabled"] = True

    # ── 状态恢复 ─────────────────────────────────────────────────────────
    # conversation.json 读写统一走 core/conversation_store（2026-07-08 修复：
    # 之前这里自带一份"假定 list 格式"的解析，chat.py 那份假定 dict——两个入口
    # 互相读不了对方写的文件，换入口续连直接崩）。
    from core.conversation_store import load_conversation
    # Restore durable state metadata even when a newer messages checkpoint is
    # selected below.  Previously the checkpoint branch skipped
    # conversation.json entirely and silently lost hook_state, including
    # pending reviewer decisions.
    restored_conversation = load_conversation(state)
    from shared.tools.run_node import recover_interrupted_decision_actions
    recover_interrupted_decision_actions(state)

    messages: list[LLMMessage]
    if args.resume:
        # 1) 优先 messages_checkpoint.json（v0.7 加的 per-turn）
        cp = load_messages_checkpoint(state)
        if cp is not None:
            messages, last_turn = cp
            print(f"=== RESUME from messages_checkpoint (project={args.project}, "
                  f"last turn={last_turn}, {len(messages)} messages) ===")
        else:
            # 2) fallback：conversation.json（chat.py / 本脚本 turn-end 写的）
            loaded = restored_conversation
            if loaded is not None:
                messages = loaded
                print(f"=== RESUME from conversation.json (project={args.project}, "
                      f"{len(messages)} messages) ===")
            else:
                print(f"=== --resume 但没找到 checkpoint，冷启动 (project={args.project}) ===")
                messages = build_messages(harness, state, node_inputs={})
        state.append_transcript("e2e_dogfood_resume",
                                  loaded_messages=len(messages))
    else:
        # 新跑：可能续连同 project_id 的 conversation.json（chat.py 或上次 dogfood 写的）
        loaded = restored_conversation
        if loaded is not None:
            messages = loaded
            print(f"=== 续连项目 {args.project!r}（{len(messages)} 条历史） ===")
        else:
            messages = build_messages(harness, state, node_inputs={})
            print(f"=== 新对话 {state.run_id}（project={args.project}） ===")
        state.append_transcript("e2e_dogfood_start",
                                  prompt_preview=(args.prompt or "")[:200])

    # ── 注入 user message ──────────────────────────────────────────────
    if args.inject:
        messages.append(LLMMessage(role="user", content=args.inject))
        print()
        print("─" * 60)
        print("INJECTED USER MESSAGE (resume mode):")
        print(args.inject)
        print("─" * 60)
    elif args.prompt:
        messages.append(LLMMessage(role="user", content=args.prompt))
        print()
        print("─" * 60)
        print("USER PROMPT:")
        print(args.prompt)
        print("─" * 60)
    print()

    # ── 跑（v3.2：瞬时网络错自动 resume）──────────────────────────────────
    result, messages = await run_with_auto_resume(harness, state, messages, llm)

    while result.status == "paused":
        print()
        print("⏸  orchestrator paused → drive_pause_chain（auto-approve）...")
        final_text = await drive_pause_chain(
            ask_fn=None,
            finalize_fn=finalize_run,
        )
        if final_text:
            result.final_text = final_text
            result.status = "completed"
        break

    print()
    print("=" * 60)
    print("ORCHESTRATOR REPLY:")
    print("=" * 60)
    print(result.final_text or "(空)")
    print("=" * 60)
    print(f"\nstatus={result.status} | turns={result.turns} | "
          f"tool_calls={len(result.tool_calls)}")
    print(f"\n💡 任何时候要中途控制：")
    print(f"   python -m core.signal inject {args.project} \"<message>\"")
    print(f"   python -m core.signal pause  {args.project}")
    print(f"   python -m core.signal abort  {args.project}")

    _save_conversation(state, messages)
    return 0 if result.status in ("completed", "paused") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
