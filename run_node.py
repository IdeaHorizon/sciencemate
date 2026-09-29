"""本地 runner —— 端到端执行单个节点。

用法：
    python run_node.py --harness literature --inputs '{"research_question": "..."}'

    # 用 YAML fixture 预注入上游上下文：
    python run_node.py --harness analysis --fixture nodes/analysis/fixtures/minimal.yaml

    # 单节点测试也支持中途打断：节点跑的时候你直接在 terminal 输入即可：
    #   - 含"停/取消/cancel/stop"等关键词 → cancel_node
    #   - 其它任何文本 → inject_into_node（作 system message 给节点）
    #   - 节点主动 pause（request_human_input）→ 你输入的就是答复，自动 resume

它做了什么：
    1. 加载指定 node 的 harness（从 nodes/{node}/harness.yaml）。
    2. 在 ~/.harness-framework/runs/ 下创建一个新的 state 目录。
    3. 如果给了 fixture，则预注入上游 artifact + memory。
    4. 端到端跑 agent loop（真实调用 LLM）。
    5. 打印 summary；完整的 transcript + artifact 落到 ~/.harness-framework/runs/{run_id}/。

测试模式说明 (fake-orchestrator)：单节点测试时没有真 orchestrator 决策 user 输入
怎么处理 —— 这里走简单 heuristic（关键词判断 cancel vs inject）。生产用 chat.py
有真 orchestrator LLM 决策 → 包装 user 反馈成精确指令。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
import os
import re
import sys
from pathlib import Path

import yaml

# 把当前目录加进 path，让 `core.*` / `shared.*` / `nodes.*` 都能 import。
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv  # noqa: E402

from core.bootstrap import bootstrap  # noqa: E402
from core.executor import execute_node, finalize_run  # noqa: E402
from shared.tools.mcp_loader import load_mcp_servers, stop_mcp_servers  # noqa: E402


# 单节点测试 fake-orchestrator 用的 cancel 关键词（包含任一就走 cancel_node）
_CANCEL_KEYWORDS = re.compile(
    r"(停了|停掉|取消|别干|算了|cancel|stop|halt|abort|kill\b)",
    re.IGNORECASE,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--harness", required=True,
                   help="要运行的 node type（如 literature、hypothesis、analysis）。")
    p.add_argument("--inputs", default="{}",
                   help='node_inputs 的 inline JSON（如 \'{"research_question": "..."}\'）。')
    p.add_argument("--fixture", type=Path, default=None,
                   help="可选的 YAML fixture，含 project/node_inputs/upstream_artifacts/memory。")
    p.add_argument("--state-dir", type=Path, default=None,
                   help="run-local 状态目录。默认 STATE_DIR 环境变量；否则 ~/.harness-framework/runs/。")
    p.add_argument("--project-id", default=None,
                   help=("项目 id：让 memory + KB 跨 run 持久化到 "
                         "$HARNESS_FRAMEWORK_HOME/projects/{project_id}/ "
                         "（默认 ~/.harness-framework/projects/{project_id}/）。"
                         "不传 → memory + KB 跟 artifact 一样 run-local。"))
    p.add_argument("--mcp-config", type=Path, default=Path("mcp_servers.yaml"),
                   help="MCP server 配置文件。文件不存在则跳过 MCP（默认 mcp_servers.yaml）。")
    p.add_argument("--sandbox", action="store_true",
                   help="临时 home：起一个 /tmp/hf-sandbox-... dir 当 HARNESS_FRAMEWORK_HOME，"
                        "跑完不污染你本机 KB。dev 测节点专用。结束打印路径方便 cd / hf status --home。")
    p.add_argument("--no-interactive", action="store_true",
                   help="禁用异步打断 / pause 交互（CI / scripted 测试用，遇 pause 直接退出）。")
    p.add_argument("--bypass-permissions", action="store_true",
                   help="⚠️ 关闭高危命令拦截（run_bash/execute_python 命中 rm -rf 等模式"
                        "不再问人，直接执行）。dogfood/自动化脚本用；交互开发慎用。")
    p.add_argument("--verbose", action="store_true", help="详细日志。")
    return p.parse_args()


def _load_fixture(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"找不到 fixture：{path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _stdin_listener(loop: asyncio.AbstractEventLoop,
                     queue: asyncio.Queue[str]) -> None:
    """Daemon 线程：阻塞读 stdin，通过 call_soon_threadsafe 投递到 asyncio queue。

    为什么是 daemon thread 而不是 asyncio.to_thread(input)：
      to_thread 把 input() 扔进 default executor 的非 daemon 线程；当 main task
      完成调 stdin_task.cancel() 时，task 的 CancelledError 只能在下一次 await
      点生效，而 executor 线程仍阻塞在 input() 系统调用上没法被打断。解释器
      退出时要等所有非 daemon 线程 join → user 必须按一下回车让 input() 返回
      线程才退出，体感就是"跑完了还得回车"。
    Daemon 线程在解释器退出时被强制终止，不会等 stdin，直接回 shell。
    """
    while True:
        try:
            line = input("")
        except (EOFError, KeyboardInterrupt):
            loop.call_soon_threadsafe(queue.put_nowait, "__INTERRUPT__")
            return
        loop.call_soon_threadsafe(queue.put_nowait, line)


async def _fake_orchestrator_handle_interrupt(
    user_text: str, *, paused_event: asyncio.Event,
    pause_answer_queue: asyncio.Queue[str],
) -> None:
    """单节点测试模式：user 中途输入怎么处理（没有真 orchestrator LLM）。

    路由：
      - 处于 pause（节点在等答复）→ 路由到 pause_answer_queue（直接当答复）
      - 否则 → heuristic：含 cancel 关键词 → cancel_node；其它 → inject_into_node
    作用的 child run = 唯一 active run（单节点测试只有 1 个 root run）。

    inject 包装：v2.x dogfood 实测 raw user 原话 inject 给 reasoning model 时
    （GLM/o1/R1）会被 "noted but continue plan" 无视。生产路径 chat.py 里
    orchestrator LLM 会把 user 原话翻译成命令式 directive 再 inject —— child
    看到的是"权威指令"形态。这里 fake_orch 没 LLM，模拟同样行为：静态包装
    一层"必须立即响应"前缀 + 把 user 原话当成 directive 输入。framework
    agent_loop 不再二次包装（保持 caller 责任原则）。
    """
    from core.pause import list_active_runs
    from core.tool_registry import execute as execute_tool

    user_text = user_text.strip()
    if not user_text:
        return

    if paused_event.is_set():
        # 节点 pause 中 —— 当作答复
        await pause_answer_queue.put(user_text)
        return

    active = list_active_runs()
    if not active:
        print("\n（你输入了，但当前没有 active child run —— 已忽略）\n")
        return
    # 单节点测试通常只有 1 个；取最后一个 active
    target = active[-1]

    # 自己当 orchestrator：拿一个 placeholder State 来调工具（工具需要 caller state
    # 写 transcript）。这里 hack：直接借用 target.state 自己写 transcript。
    if _CANCEL_KEYWORDS.search(user_text):
        result = await execute_tool(
            "runtime_control", target.state,
            action="cancel",
            child_run_id=target.run_id,
            reasoning=f"[test-mode user interrupt] {user_text}",
        )
        ok = result.get("status") == "success"
        print(f"\n[test → {target.run_id}] {'⛔ cancel' if ok else '✗ cancel 失败'}: {user_text[:120]}\n")
    else:
        # ★ 包装 user 原话成 directive 形态（模拟 production orchestrator 的工作）
        wrapped = (
            f"⚠️ 用户在 run_node.py 调试模式下中途打断（最高优先级）—— "
            f"**必须立即响应这条**，不要默默 noted 后继续原 plan。\n\n"
            f"用户原话：{user_text}\n\n"
            f"处理要求：\n"
            f"1. 如是问题 / 质疑 → 立刻调工具核查 + 直接回答；不要嵌在无关上下文里跳过。\n"
            f"2. 如是改方向 / cancel 意图 → 立刻调整后续 plan 或调 request_human_input 确认。\n"
            f"3. 不允许只 acknowledge 然后继续原 multi-step plan —— "
            f"用户特意打断说明他认为当前路径有问题。"
        )
        result = await execute_tool(
            "runtime_control", target.state,
            action="inject",
            child_run_id=target.run_id,
            content=wrapped,
            source="test_fake_orchestrator",
        )
        ok = result.get("status") == "success"
        print(f"\n[test → {target.run_id}] {'📨 inject' if ok else '✗ inject 失败'}: {user_text[:120]}\n")


async def _race_main_task_with_input(
    main_task: asyncio.Task,
    input_queue: asyncio.Queue[str],
    *,
    paused_event: asyncio.Event,
    pause_answer_queue: asyncio.Queue[str],
) -> None:
    """跟 chat.py 主控同款：跑 main_task 的同时监听 stdin → inject / cancel。

    main_task 完成时 return（不取消，让 caller 拿 result）。
    """
    while not main_task.done():
        next_input_task = asyncio.create_task(input_queue.get())
        done, _pending = await asyncio.wait(
            [main_task, next_input_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if next_input_task in done:
            line = next_input_task.result()
            if line == "__INTERRUPT__":
                print("\n（捕获中断 —— 让节点完成当前轮再退出。直接 Ctrl-C 二次强退）")
                continue
            await _fake_orchestrator_handle_interrupt(
                line,
                paused_event=paused_event,
                pause_answer_queue=pause_answer_queue,
            )
        else:
            next_input_task.cancel()
            try:
                await next_input_task
            except (asyncio.CancelledError, Exception):
                pass


async def _drive_with_interaction(
    summary_initial: dict, *, no_interactive: bool,
    input_queue: asyncio.Queue[str] | None = None,
    paused_event: asyncio.Event | None = None,
    pause_answer_queue: asyncio.Queue[str] | None = None,
) -> dict:
    """节点 status=paused 时进 pause_driver 交互式答 pause 问题。

    可选三个 queue 参数：调用方（_main）已经起了 stdin daemon thread + queue 时
    传进来共用，避免多 listener 抢同一 stdin。**测试 / 老代码不传 = 自起一份**
    （只在 paused 时才起，跟原行为兼容）。

    返回最终 summary dict（可能经过 pause/resume + injects + 多次 cancel）。
    """
    if no_interactive:
        if summary_initial.get("status") == "paused":
            print("⚠️  节点 paused 但 --no-interactive 启用，直接退出（不答 pause）。")
        return summary_initial

    if summary_initial.get("status") != "paused":
        return summary_initial

    from core.pause_driver import drive_pause_chain
    from core.pause import PauseEvent

    # back-compat：调用方没传 queue → 自起一份（旧测试路径）
    own_listener = input_queue is None
    if own_listener:
        input_queue = asyncio.Queue()
        paused_event = asyncio.Event()
        pause_answer_queue = asyncio.Queue()
        threading.Thread(
            target=_stdin_listener,
            args=(asyncio.get_running_loop(), input_queue),
            daemon=True,
        ).start()
    else:
        assert paused_event is not None and pause_answer_queue is not None

    # ── 已 paused：进入交互式 pause + interrupt 循环 ────────────────────
    print()
    print("=" * 60)
    print("⏸  节点产生了 pause（request_human_input）—— 进入交互模式。")
    print("    输入答复回车，节点会 resume。也可中途插话 redirect / cancel。")
    print("=" * 60)

    async def ask_via_queue(pe: PauseEvent) -> str:
        paused_event.set()
        try:
            _print_pause_prompt(pe)
            return await pause_answer_queue.get()
        finally:
            paused_event.clear()

    drive_task = asyncio.create_task(
        drive_pause_chain(ask_fn=ask_via_queue, finalize_fn=finalize_run)
    )
    await _race_main_task_with_input(
        drive_task, input_queue,
        paused_event=paused_event,
        pause_answer_queue=pause_answer_queue,
    )

    # drive 完成 → finalize_fn 已写 summary.json，重读
    from pathlib import Path as _P
    state_dir_str = summary_initial.get("state_dir", "")
    if state_dir_str:
        p = _P(state_dir_str) / "summary.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return summary_initial


def _print_pause_prompt(pe) -> None:
    print()
    print("=" * 60)
    asking = pe.asking_node_type or "?"
    print(f"[需要人工输入]（来自节点：{asking}）")
    print(f"问题：{pe.question}")
    if pe.context:
        print(f"\n背景：\n{pe.context}")
    if pe.options:
        print("\n选项：")
        for i, opt in enumerate(pe.options, 1):
            print(f"  [{i}] {opt}")
        print("  （或自由输入任意文本）")
    print("=" * 60)
    print("（直接输入答复，会路由给暂停的节点；或输入 redirect 指令做 inject/cancel）")


async def _main() -> int:
    args = _parse_args()
    load_dotenv()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("httpcore", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.bypass_permissions:
        from shared.lib import dangerous_commands as _dc
        _dc.set_bypass_mode(True)
        print("⚠️  --bypass-permissions：高危命令拦截已关闭，run_bash/execute_python 不再问人确认。")

    # 沙盒模式：临时 HARNESS_FRAMEWORK_HOME，跑完不污染本机 KB
    sandbox_path: Path | None = None
    if args.sandbox:
        import tempfile
        sandbox_path = Path(tempfile.mkdtemp(prefix=f"hf-sandbox-{args.harness}-"))
        os.environ["HARNESS_FRAMEWORK_HOME"] = str(sandbox_path)
        os.environ["HARNESS_FRAMEWORK_ORG_HOME"] = str(sandbox_path / "org")
        print(f"🧪 sandbox mode: HARNESS_FRAMEWORK_HOME = {sandbox_path}")

    # 注册全部工具 + skill（共享 + 节点专属）
    bootstrap()

    fixture = _load_fixture(args.fixture) if args.fixture else {}

    inline_inputs = json.loads(args.inputs) if args.inputs else {}
    node_inputs = {**fixture.get("node_inputs", {}), **inline_inputs}

    from core.paths import runs_root
    state_dir = args.state_dir or Path(os.getenv("STATE_DIR") or runs_root())
    state_dir.mkdir(parents=True, exist_ok=True)

    # project_id 优先级：CLI > fixture > env > None（不持久化）
    project_id = (
        args.project_id
        or fixture.get("project_id")
        or os.getenv("HARNESS_FRAMEWORK_PROJECT_ID")
    )

    mcp_clients = await load_mcp_servers(args.mcp_config)
    try:
        if args.no_interactive:
            # CI / scripted —— 直接 await，不监听 stdin
            summary = await execute_node(
                node_type=args.harness,
                state_dir=state_dir,
                project_id=project_id,
                node_inputs=node_inputs,
                upstream_artifacts=fixture.get("upstream_artifacts"),
                upstream_memory=fixture.get("memory_entries"),
            )
            summary = await _drive_with_interaction(
                summary, no_interactive=True,
            )
        else:
            # 交互模式：起 stdin daemon thread + queue 一次（全程共用）；
            # execute_node 包成 task，跟 input_queue race，所以**节点跑中也能
            # inject / cancel**（修 #N：以前只在 paused 时才监听 stdin）。
            input_queue: asyncio.Queue[str] = asyncio.Queue()
            paused_event = asyncio.Event()
            pause_answer_queue: asyncio.Queue[str] = asyncio.Queue()
            threading.Thread(
                target=_stdin_listener,
                args=(asyncio.get_running_loop(), input_queue),
                daemon=True,
            ).start()

            exec_task = asyncio.create_task(execute_node(
                node_type=args.harness,
                state_dir=state_dir,
                project_id=project_id,
                node_inputs=node_inputs,
                upstream_artifacts=fixture.get("upstream_artifacts"),
                upstream_memory=fixture.get("memory_entries"),
            ))
            await _race_main_task_with_input(
                exec_task, input_queue,
                paused_event=paused_event,
                pause_answer_queue=pause_answer_queue,
            )
            summary = exec_task.result()

            # 节点 paused → 进 pause_driver 答 pause；继续复用同一份 queue
            summary = await _drive_with_interaction(
                summary, no_interactive=False,
                input_queue=input_queue,
                paused_event=paused_event,
                pause_answer_queue=pause_answer_queue,
            )
    finally:
        await stop_mcp_servers(mcp_clients)

    print()
    print("=" * 60)
    print(f"Run {summary['run_id']} —— {summary['node_type']}")
    print("=" * 60)
    print(f"  status:   {summary['status']}")
    if summary.get("failure_category") == "provider_tool_call_protocol_error":
        print("  ⚠️  provider/tool-call protocol failure: model returned DSML "
              "fragments but no structured tool_calls —— 这是 provider 兼容性问题，"
              "不是节点 prompt/质量问题。")
    if summary.get("status") == "cancelled" and summary.get("cancel_meta"):
        cm = summary["cancel_meta"]
        print(f"  cancel:   {cm.get('reason')} (by {cm.get('requested_by')} @ turn {cm.get('cancelled_at_turn')})")
    print(f"  turns:    {summary['turns']}")
    print(f"  tools:    {summary['tool_call_count']}")
    print(f"  outputs:  {[a['type'] for a in summary['artifacts']]}")
    if summary['missing_required_outputs']:
        print(f"  缺失的必需产出：{summary['missing_required_outputs']}")
    print(f"  state:    {summary['state_dir']}")
    if project_id:
        print(f"  project:  {project_id}（memory + KB 持久化）")
    if sandbox_path is not None:
        print()
        print(f"🧪 sandbox kept at: {sandbox_path}")
        print(f"   inspect:  HARNESS_FRAMEWORK_HOME={sandbox_path} hf status")
        print(f"   cleanup:  rm -rf {sandbox_path}  (or `hf sandbox prune`)")
    print()
    print("最终 assistant 文本（预览）：")
    print(summary["final_text_preview"])
    return 0 if summary["status"] == "completed" else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
