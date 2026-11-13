"""ScienceMate terminal UI. Run with python -m cli --project PROJECT."""

from __future__ import annotations

import argparse
import asyncio
import atexit
import logging
import os
import sys
import threading
from pathlib import Path

import chat as runtime
from core.naming import canonical_node_type
from shared.tools.mcp_loader import load_mcp_servers, stop_mcp_servers

from . import commands, terminal
from . import input as input_io


async def _run_repl() -> int:
    input_io.enable_line_editing()
    # dotenv is a terminal convenience.  The App Server imports this module
    # with credentials supplied only to its isolated Worker process.
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--project",
        "--project-id",
        dest="project_id",
        default=None,
        help="项目 id（决定 memory + KB 持久化目录 + 对话续连）。",
    )
    parser.add_argument(
        "--session",
        dest="session_id",
        default=None,
        help="会话 id。指定后 CLI 在该项目下开一个自己的 Git "
        "worktree（分支 session/<id>），与 UI 正在跑的会话"
        "并行且互不抢写道。不指定则直接用项目主工作区。",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="run-local 状态目录。默认 STATE_DIR 环境变量；否则 ~/.harness-framework/runs/。",
    )
    parser.add_argument("--mcp-config", type=Path, default=Path("mcp_servers.yaml"))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--autonomy",
        choices=("assisted", "autonomous", "continuous"),
        default=None,
        help="启动档位，与 UI 三档同一份词表。assisted=需要用户判断时再问；"
        "autonomous=只在高危点停；continuous=⚠️ 预授权全部高危类别、"
        "不停。REPL 中可用 /autonomy 切换。"
        "不给则沿用这个会话上次的声明。",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="♾️ 等价于 --autonomy continuous 再加自动续轮：顶层 turn 结束、"
        "空回复、异常或后台节点完成后自动续轮，"
        "直到 complete / human-only blocked / /stop。",
    )
    args = parser.parse_args()

    # 抓终端快照（在任何 input()/readline 动 TTY 之前）+ 注册 atexit 复原：
    # 保证 /exit / Ctrl-C / 异常退出后终端都能回到 canon+echo（见文件顶部注释）。
    _saved_term = terminal._save_terminal_state()
    if _saved_term:
        atexit.register(terminal._restore_terminal_state, _saved_term)

    load_dotenv()

    # v0.8: 项目嵌套 runs。有 project_id → projects/<id>/runs/；无 → runs_anon/
    # --state-dir 覆盖仍尊重（fixtures / 单测 / 离线 debug 用）
    from core.paths import runs_parent

    if args.state_dir is not None:
        base_dir = args.state_dir
    elif os.getenv("STATE_DIR"):
        # [deprecated] STATE_DIR env var：v0.8 起仅 backward-compat；旧 ./output/
        # 用 STATE_DIR 重定向走老 flat 布局。新装别用，走 runs_parent 项目嵌套。
        base_dir = Path(os.getenv("STATE_DIR"))
    else:
        base_dir = runs_parent(args.project_id)
    base_dir.mkdir(parents=True, exist_ok=True)

    # ── Project Git 工作区（v2.1 现行架构）────────────────────────────────
    # 平台入口一直传这个参数，CLI 入口一直没传 —— 于是全仓 70+ 处
    # `if state.project_worktree is not None:` 在 CLI 下永远走 else 分支。
    # 那些 else 是 v1 遗留，而两条路只有一条被维护：实测同一套代码，CLI 项目
    # 攒下 87 条记忆，平台项目一条都没有（记忆的持久化写在 else 里，if 那边只
    # "报给 orchestration" 就返回了）。
    #
    # 让 CLI 也绑，两件事同时成立：CLI 用上现行架构，那些 else 分支变成可删的
    # 死代码 —— 而不是永远要给每个断点补一个"平台分支"。
    project_worktree = None
    if args.project_id:
        from core.project_bootstrap import (
            ProjectBootstrapError,
            ensure_project_worktree,
            open_session_worktree,
        )

        try:
            if args.session_id:
                # 一个入口 = 一个 session。UI 已经是这么工作的（每个 Session
                # 一个 worktree + session/<id> 分支），CLI 用同一套约定接入，
                # 于是"同一个项目、两个入口"成立而不互相抢 Git 写道。
                project_worktree = open_session_worktree(args.project_id, args.session_id)
            else:
                project_worktree = ensure_project_worktree(args.project_id)
        except ProjectBootstrapError as exc:
            # 建不出工作区不该让人进不去项目 —— 但也绝不能静默降级到旧路径，
            # 那会变成"看起来在跑，记忆却存不下"。说清楚再继续。
            print(f"⚠️  无法准备 Project 工作区，本次以旧模式运行：{exc}")

    state = runtime._make_or_load_orchestrator_state(
        args.project_id,
        base_dir,
        project_worktree=project_worktree,
    )

    # 会话锁：同 project 双开会互相覆盖 conversation.json，直接拒绝第二个实例
    if not runtime._acquire_session_lock(state):
        print(
            f"⛔ 项目 {args.project_id or '(anon)'} 已有一个 chat.py 实例在跑"
            f"（锁：{state.root / '.chat.lock'}）。\n"
            "   双开会互相覆盖对话历史。请用那个实例，或先退出它。"
        )
        return 2

    # ── 日志全部进文件，终端只留对话流（2026-07-08 UX 重整）──────────────
    # 之前 numexpr/torch/embeddings/子节点 runtime_trace 全刷进聊天终端，
    # `你 ›` 提示符被日志埋掉、对话没法读。现在：文件收全量（chat.log），
    # 终端只透 ERROR。--verbose 恢复旧行为（终端全量 + 文件）。
    log_path = state.root / "chat.log"
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _fh = logging.FileHandler(log_path, encoding="utf-8")
    _fh.setFormatter(_fmt)
    _fh.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    _ch = logging.StreamHandler()
    _ch.setFormatter(_fmt)
    _ch.setLevel(logging.DEBUG if args.verbose else logging.ERROR)
    _root = logging.getLogger()
    _root.handlers.clear()
    _root.addHandler(_fh)
    _root.addHandler(_ch)
    _root.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    for noisy in ("httpcore", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not args.verbose:
        # torch 等库用 warnings 模块直写 stderr（不走 logging），一并静音
        import warnings as _warnings

        _warnings.filterwarnings("ignore", category=FutureWarning)

    runtime.bootstrap()

    # P1-8：注册 live 进度回调（turn 进行中每个工具调用打一行 dim 摘要）
    runtime._agent_loop.set_progress_sink(terminal._print_progress)

    # 档位不在这里施加 —— 声明还在磁盘上（conversation load 之后才在手上），
    # 而开关是声明的投影，不能先于声明存在。施加点见下面 load 之后那一段。
    harness = runtime.load_harness("_orchestrator")
    llm = runtime.LLMClient()

    # 续连或冷启动
    existing = runtime._load_conversation(state)
    is_continue = existing is not None
    if is_continue:
        messages = existing
        from shared.tools.run_node import recover_interrupted_decision_actions

        recover_interrupted_decision_actions(state)
    else:
        messages = runtime.build_messages(harness, state, node_inputs={})

    protocol_repairs = runtime._repair_message_tool_protocol(messages)
    if protocol_repairs:
        state.append_transcript(
            "conversation_protocol_repaired",
            repairs=protocol_repairs,
            source="session_start",
        )

    # conversation load 会恢复 hook_state，所以 continuous 必须在 load 之后重放。
    # CLI 显式传 flag = 无论上次 phase 是 complete/blocked 都重新启动；仅靠持久化
    # 恢复时则保留 phase（crash 中断的 running 会自动续，已 complete 不会复活）。
    # 档位先施加（它是"怎么跑"的真相源；持久化的声明在 load 之后才在手上），
    # 再决定续轮。--continuous 是用户在自己终端里说的"一路跑到底"，两件事都要。
    # 没给 flag 就沿用这个会话上次的声明 —— 施加这一步永远要做，因为进程刚起来
    # 时那三个开关是环境变量的默认值，不是这个会话的档位。
    from core.session_driver import autonomy_mode as _autonomy_mode

    chosen_mode = (
        "continuous"
        if args.continuous
        else args.autonomy
        if args.autonomy
        else _autonomy_mode(state)
    )
    runtime._set_autonomy_mode(state, chosen_mode)
    persisted_loop = runtime._continuous_loop(state)
    if args.continuous:
        runtime._set_continuous_loop(state, True, reset_phase=True)
    elif persisted_loop:
        runtime._set_continuous_loop(state, True, reset_phase=False)

    if args.continuous:
        print("♾️  --continuous：自动续轮已开启，档位 = 连续（预授权全部高危类别）。")

    state.append_transcript("chat_session_start", project_id=args.project_id)

    # dreaming 状态摘一行（进 banner 的「记忆」行）
    dreaming_line: str | None = None
    try:
        from core.loop_hooks_builtin import _check_dreaming_due

        is_due, last_at = _check_dreaming_due(state, max_age_days=7)
        if is_due:
            if last_at:
                dreaming_line = terminal._amber(f"💤 上次整理 {last_at}（>7天）") + terminal._dim(
                    " · 说 'dream' 可再整理"
                )
            else:
                dreaming_line = terminal._amber("💤 从未整理过") + terminal._dim(
                    " · 说 'dream' 让它扫 KB 找过期/synthesis/opportunity"
                )
    except Exception:
        pass

    terminal._print_startup_banner(
        run_id=state.run_id,
        project_id=args.project_id,
        is_continue=is_continue,
        n_messages=len(messages),
        model=os.getenv("LLM_MODEL", "?"),
        dreaming_line=dreaming_line,
    )
    if not args.verbose:
        print(
            terminal._dim(f"   日志 → {log_path}（终端只显示对话；--verbose 恢复全量日志）") + "\n"
        )

    # curator 退出 post-producing flow 后（wangd 2026-08-19），"上次会话留了 N 条
    # 待整合 KB"这条提醒随之删除 —— 它提醒的是一个不再存在的欠账：curator 是按需
    # 调取的后台节点，没跑过它不构成"欠着"。

    # 崩溃遗留 run surfacing（R0-c）：上次进程死在半路的 run 不能静默消失 ——
    # 提示用户 + 注入给模型（用户问起时模型知道有这回事、能提议重跑/收尾）。
    orphans = runtime._scan_orphaned_runs(base_dir, state.run_id)
    if orphans:
        print(terminal._amber(f"⚠️  发现 {len(orphans)} 个上次会话中断遗留的 run："))
        for o in orphans[:5]:
            kind_txt = "崩溃时正等人工答复" if o["kind"] == "paused_orphan" else "跑到一半进程退出"
            print(terminal._dim(f"     - {o['run_id']}（{kind_txt}）"))
        print(terminal._dim("   跟 orchestrator 说「处理一下遗留的 run」可让它检查/重跑。") + "\n")
        # 无人值守（continuous）时没人会"问起" —— 指令必须是主动的，否则
        # 遗留 run 只是被报了一嗓子就永远躺在那儿（E2E-5b 实测：重启留下的
        # 两个在飞 experiment 孤儿挂了 3+ 小时没人管）。
        _orphan_list = "; ".join(f"{o['run_id']}({o['kind']})" for o in orphans[:5])
        if runtime._continuous_running(state):
            _orphan_instr = (
                "**你现在就处理它们**（这是本轮的第一优先级）：逐个读 state_dir "
                "下的 transcript 判断进度 —— 已实质完成的按其产物收尾入账；跑了"
                "一半的评估要不要重跑（结合它已花的成本）；不值得续的明确放弃并"
                "说明理由。不要假装它们不存在。"
            )
        else:
            _orphan_instr = (
                "用户若问起或要求处理：读对应 state_dir 下的 summary/transcript "
                "判断进度，提议重跑或收尾；不要假装它们不存在。"
            )
        state.hook_state.setdefault("injected_messages", []).append(
            {
                "content": (
                    f"[启动检查] 发现 {len(orphans)} 个上次会话中断遗留的 "
                    f"run：{_orphan_list}。{_orphan_instr}"
                ),
                "source": "startup_orphan_scan",
            }
        )

    mcp_clients = await load_mcp_servers(args.mcp_config)

    chat_state = runtime.ChatState()
    threading.Thread(
        target=input_io._stdin_listener,
        args=(asyncio.get_running_loop(), chat_state.input_queue),
        daemon=True,
    ).start()
    # 续连项目在 continuous=running 时无需等人再敲一句“继续”。冷启动项目尚无
    # objective，仍等第一条真实 user prompt；那一轮**起了研究**之后才自动续 ——
    # 第一句话不一定是目标（「你好」），见 `_nothing_to_continue`。
    if is_continue and runtime._continuous_running(state):
        boot_prompt, _ = runtime._continuous_followup(
            state, "CONTINUOUS_STATUS: continue", reason="session_start"
        )
        if boot_prompt:
            chat_state.input_queue.put_nowait(boot_prompt)
    terminal._PROMPT_LIVE["on"] = True  # 此后底部输入行归线程；输出走 _emit_above_prompt
    _real_stderr = sys.stderr
    sys.stderr = terminal._PromptSafeStderr(_real_stderr)  # 接管裸 print(file=sys.stderr)，见类注释

    # R1：后台子节点事件（完成/暂停/失败）即时打印（_print_speaker 会重画提示符）
    def _on_child_event(ev: dict) -> None:
        kind = ev.get("kind")
        nt = canonical_node_type(ev.get("node_type", "?"))
        rid = ev.get("run_id", "")
        if kind == "completed":
            imported = ev.get("imported") or []
            terminal._print_speaker(
                f"后台 {nt}",
                f"✅ 完成（status={ev.get('status')}, turns={ev.get('turns')}）"
                + (f"，产出 {len(imported)} 个 artifact" if imported else ""),
            )
        elif kind == "paused":
            terminal._print_speaker(
                f"后台 {nt}",
                f"⏸ [{rid}] 在等你输入：{(ev.get('question') or '')[:200]}\n"
                f"   用 /answer <你的答复> 直接回给它。",
            )
        else:
            terminal._print_speaker(f"后台 {nt}", f"❌ 失败：{ev.get('error', '?')}")

        # background run 完成后 parent 没有天然新 user turn。continuous 模式在
        # idle 时机械唤醒；若 parent 正在跑，它自己会消费 child event，不重复排队。
        if (
            kind in ("completed", "failed")
            and runtime._continuous_running(state)
            and not chat_state.turn_running.is_set()
            and chat_state.input_queue.empty()
        ):
            prompt, _ = runtime._continuous_followup(
                state,
                "CONTINUOUS_STATUS: continue",
                reason=f"background_{kind}",
            )
            if prompt:
                chat_state.input_queue.put_nowait(prompt)

    from shared.tools.run_node import set_child_event_sink

    set_child_event_sink(_on_child_event)

    # R2-b 流式逐 token 显示：只挂在 orchestrator 自己的 llm 实例上，子节点用
    # 各自新建的 LLMClient() → 不会把子节点 token 刷到用户屏幕。header（🔬
    # Orchestrator）每个 user-turn 只打一次，由 stream_state 控制。
    _turn_stream = {"header_shown": False}

    def _stream_display(delta: str | None) -> None:
        if delta is None:  # 本次 LLM 响应流结束
            terminal._stream_end()
            return
        header = None
        if not _turn_stream["header_shown"]:
            header = f"\n{terminal._orchestrator_header()}"
            _turn_stream["header_shown"] = True
        _turn_stream["shown"] += delta
        terminal._stream_write(delta, header=header)

    llm.stream_display = _stream_display
    # 推理阶段可见化（issue #166）：思考期间不再全黑。走独立通道，不碰
    # header_shown —— 思考指示不算"回复已开始"，正文来了照样打 header。
    llm.stream_reasoning_display = terminal._reasoning_display

    # R2-b：/stop 时流式 LLM 生成 chunk 级中止（不用等一次完整生成）
    from core.llm import set_stream_abort_check

    set_stream_abort_check(chat_state.panic.is_set)

    # Phase F: 检测 dreaming pending，启动时自动后台跑（不审批 / 显示进度）
    dreaming_task: asyncio.Task | None = None
    if args.project_id:
        try:
            from core.dreaming_scheduler import should_run_dreaming

            should_dream, reasons = should_run_dreaming(args.project_id)
            if should_dream:
                terminal._emit_above_prompt(
                    terminal._dim("💤 后台整理记忆中… ")
                    + terminal._dim(f"（{', '.join(reasons[:3])}；不阻塞对话）")
                )
                dreaming_task = asyncio.create_task(
                    runtime._run_dreaming_background(
                        state, llm, args.project_id, chat_state=chat_state
                    )
                )
        except Exception as e:
            logging.warning("dreaming auto-trigger 失败：%s", e)

    try:
        while True:
            # 提示符由 stdin 线程的 input(_USER_PROMPT_RL) 持有并显示（不再主协程
            # 单独 print）——这样退格受 readline 保护，不会抹掉 `你 ›`。
            user_text = await chat_state.input_queue.get()
            user_text = user_text.strip()
            if not user_text:
                continue

            is_continuous_turn = runtime._is_continuous_turn(user_text)

            if user_text in ("/exit", "/quit"):
                break
            if commands._is_slash_command(user_text):
                wants_exit = await commands._handle_slash(user_text, state, messages)
                if wants_exit:
                    break
                if state.hook_state.pop("continuous_kick_requested", False):
                    prompt, _ = runtime._continuous_followup(
                        state,
                        "CONTINUOUS_STATUS: continue",
                        reason="manual_enable",
                    )
                    if prompt:
                        chat_state.input_queue.put_nowait(prompt)
                runtime._save_conversation(state, messages)
                continue

            # continuous 已完成/blocked 后，新的真实 user 消息代表新目标或明确恢复；
            # 框架内部续轮不重置 phase。
            if runtime._continuous_loop(state) and not is_continuous_turn:
                state.hook_state["continuous_phase"] = "running"
                state.hook_state["continuous_no_progress_turns"] = 0
                state.hook_state["continuous_error_turns"] = 0

            # 普通 user message → 起 orchestrator 一轮 task
            chat_state.turn_running.set()
            _turn_stream["header_shown"] = False  # 每 user-turn 只打一次 header
            _turn_stream["shown"] = ""
            # 忙碌提示（在提示符上方）：一轮可能几分钟且中途打印子节点/后台信息。
            terminal._emit_above_prompt(
                terminal._dim("⏳ 处理中…（直接输入=中途插话，/stop 停止本轮）")
            )
            turn_task = asyncio.create_task(
                runtime._run_one_turn(state, harness, messages, llm, user_text, chat_state)
            )

            # turn 跑的时候同时监听 user 中途打断
            while not turn_task.done():
                next_input_task = asyncio.create_task(chat_state.input_queue.get())
                done, _pending = await asyncio.wait(
                    [turn_task, next_input_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if next_input_task in done:
                    interrupt = next_input_task.result().strip()
                    if not interrupt:
                        continue
                    # 模式判断：
                    #   1) child 已 pause（pause_driver 在等答复）→ 路由到 pause_answer_queue
                    #   2) child 正在跑（active 未 pause）→ 跑 orchestrator interrupt 决策
                    #   3) /exit 等命令在 turn 跑中暂不支持（提示即可）
                    if interrupt in ("/exit", "/quit"):
                        terminal._emit_above_prompt(
                            "（请等当前 turn 跑完再退出；先 /stop 可尽快结束本轮；"
                            "或按 Ctrl-C 强制）"
                        )
                        continue
                    if interrupt in ("/stop", "/abort"):
                        # 🛑 panic button：kill orchestrator 本轮 + 全部 active child
                        runtime._do_panic_stop(state, chat_state)
                        continue
                    if commands._is_slash_command(interrupt):
                        # 安全子集允许 mid-turn 执行（实测痛点：child 连环撞高危
                        # 确认时最需要切 /autonomy continuous，却被一刀切拒掉）。这些命令只改
                        # 进程级开关 / hook_state / 只读查询，不碰正在运行的
                        # messages。/reset（清 messages）、/undo（agent 可能正在
                        # 改 artifact）仍禁——真正有并发冲突的只有它们。
                        if commands._midturn_slash_allowed(interrupt):
                            await commands._handle_slash(interrupt, state, messages)
                        else:
                            terminal._emit_above_prompt(
                                f"（{interrupt}：turn 跑中不支持。可用："
                                f"{' '.join(commands._MIDTURN_SAFE_SLASH)}；/stop 停止本轮）"
                            )
                        continue
                    if chat_state.paused.is_set():
                        # 路由给 pause driver
                        await chat_state.pause_answer_queue.put(interrupt)
                    else:
                        # interrupt 模式：先给即时回执（决策 LLM 要跑几秒，
                        # 没回执 user 会以为输入被吞了）
                        terminal._emit_above_prompt(
                            terminal._dim(
                                "（本轮仍在进行——你的输入已转为中途插话，"
                                "orchestrator 正在决定如何处理…）"
                            )
                        )
                        try:
                            await runtime._handle_interrupt(
                                state, harness, llm, interrupt, chat_state=chat_state
                            )
                        except Exception as e:
                            terminal._emit_above_prompt(
                                f"[ERROR interrupt 失败] {type(e).__name__}: {e}"
                            )
                            logging.exception("interrupt handling error")
                else:
                    # turn_task 完成，撤掉 input 任务
                    next_input_task.cancel()
                    try:
                        await next_input_task
                    except (asyncio.CancelledError, Exception):
                        pass

            chat_state.turn_running.clear()
            chat_state.panic.clear()  # /stop 只作用于刚结束的这一轮
            terminal._stream_end()  # 收束任何未收尾的流式段

            # 拿结果
            try:
                reply = turn_task.result()
            except Exception as e:
                terminal._emit_above_prompt(
                    f"\n[ERROR] orchestrator 跑挂了：{type(e).__name__}: {e}"
                )
                logging.exception("agent_loop error")
                err_text = str(e).lower()
                if ("tool_calls" in err_text and "tool messages" in err_text) or (
                    "tool_call_id" in err_text and "invalid" in err_text
                ):
                    repaired = runtime._repair_message_tool_protocol(messages)
                    if repaired:
                        state.append_transcript(
                            "conversation_protocol_repaired",
                            repairs=repaired,
                            source="turn_error",
                        )
                        terminal._emit_above_prompt(
                            terminal._amber(
                                f"♻️ 已修复 {repaired} 条中断遗留的 tool-call 协议，"
                                "continuous 将自动重试。"
                            )
                        )
                await runtime._queue_continuous_followup(state, chat_state, "", reason="turn_error")
                runtime._save_conversation(state, messages)
                continue

            # ── 空轮：不当"回答"处理 ──────────────────────────────────────
            # run_loop 已回滚（messages 与轮初逐字节相同）。continuous 下安排
            # 驻定重放；非 continuous 下正常显示诊断文案（用户在场，自己决定）。
            void_info = state.hook_state.pop("_void_turn_final", None)
            if void_info and runtime._continuous_loop(state):
                terminal._emit_above_prompt(
                    terminal._amber(
                        f"⚠️ 模型空响应（prompt≈{void_info.get('prompt_tokens')}tk，"
                        f"loop 内已重试 {void_info.get('retries')} 次）——上下文已回滚，"
                        "退避后原样重放。"
                    )
                )
                await runtime._queue_continuous_followup(
                    state, chat_state, reply, reason="void_turn"
                )
                runtime._save_conversation(state, messages)
                continue

            driver_status = runtime._continuous_status(reply)
            display_reply = runtime._strip_continuous_status(reply)
            if not display_reply and driver_status:
                display_reply = f"（持续运行状态：{driver_status}）"

            # 回复已在本轮流式逐 token 显示过 → 不重复打印（避免同一段出现两次）。
            # 否则（非流式 / nudge 重问 / 诚实兜底产生的新文本）正常打印。
            shown = terminal._norm_text(_turn_stream["shown"])
            rep = terminal._norm_text(display_reply)
            if not (rep and shown and rep in shown):
                terminal._print_reply(display_reply)
            else:
                terminal._emit_above_prompt("")  # 收个尾，提示符归位

            # P0-6：本轮跑中被缓存的插话，现在当新用户消息回灌队列（FIFO），
            # 下一轮 while 循环从 input_queue 取到、正常处理。
            while chat_state.deferred_inputs:
                chat_state.input_queue.put_nowait(chat_state.deferred_inputs.pop(0))

            await runtime._queue_continuous_followup(
                state, chat_state, reply, reason="turn_finished"
            )
            runtime._save_conversation(state, messages)

    finally:
        # Phase F: 等后台 dreaming task（如果有）完成或 cancel
        if dreaming_task is not None and not dreaming_task.done():
            print("💤 等后台 dreaming 跑完（按 Ctrl-C 再次中断）...")
            try:
                await asyncio.wait_for(dreaming_task, timeout=120)
            except (TimeoutError, asyncio.CancelledError):
                dreaming_task.cancel()
                try:
                    await dreaming_task
                except (asyncio.CancelledError, Exception):
                    pass

        # stdin 是 daemon thread，进程退出自动终止，不需要显式 cancel —— 但它
        # 阻塞在 readline 里会把 TTY 留在 raw 态，这里立刻复原（atexit 兜底再来一次）。
        terminal._restore_terminal_state(_saved_term)
        sys.stderr = _real_stderr  # 还原，别让包装流泄漏到 REPL 生命周期外
        await stop_mcp_servers(mcp_clients)
        runtime._save_conversation(state, messages)
        state.append_transcript("chat_session_end")
        print(f"对话已保存到 {runtime._orchestrator_state_path(state)}")

    return 0


async def _main() -> int:
    previous = runtime._terminal_output
    runtime._terminal_output = terminal
    try:
        return await _run_repl()
    finally:
        runtime._terminal_output = previous


def main() -> int:
    try:
        code = asyncio.run(_main())
    except KeyboardInterrupt:
        terminal._print_end_banner("interrupted")
        return 130
    except Exception as exc:
        logging.exception("Terminal UI exited unexpectedly")
        terminal._print_end_banner("crashed", f"{type(exc).__name__}: {exc}")
        return 1
    if code == 0:
        terminal._print_end_banner("normal")
    return code
