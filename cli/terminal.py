"""Terminal rendering only: prompts, progress, streaming and pause questions."""

from __future__ import annotations

import os
import re
import sys
import time

import chat as runtime
from core.naming import canonical_artifact_type, canonical_node_type, canonical_tool_name
from shared.lib.console_box import draw_box


def _save_terminal_state():
    """抓 stdin 的 termios 快照。非 tty / 无 termios → 返 None。"""
    try:
        import termios

        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return None
        return (fd, termios.tcgetattr(fd))
    except Exception:
        return None


def _restore_terminal_state(saved) -> None:
    """把终端 termios 复原成 saved 快照（幂等；saved 为 None 时无操作）。"""
    if not saved:
        return
    fd, attrs = saved
    try:
        import termios

        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
    except Exception:
        pass


_USE_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR") is None and os.getenv("TERM") != "dumb"


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def _dim(s: str) -> str:
    return _c("2", s)


def _bold(s: str) -> str:
    return _c("1", s)


def _cyan(s: str) -> str:
    return _c("38;5;44", s)


def _amber(s: str) -> str:
    return _c("38;5;179", s)


def _green(s: str) -> str:
    return _c("38;5;71", s)


def _print_startup_banner(
    *,
    run_id: str,
    project_id: str | None,
    is_continue: bool,
    n_messages: int,
    model: str,
    dreaming_line: str | None,
) -> None:
    """启动 banner —— CJK 宽度感知的圆角框（见 shared/lib/console_box）。"""
    label = lambda s: _dim(s.ljust(6))  # noqa: E731  固定 6 列标签，值左对齐
    rows: list[str] = [
        f"{_cyan('🔬')}  {_bold('AI4Science 研究平台')}  {_dim('·')}  Orchestrator",
        "",
    ]
    if is_continue:
        rows.append(f"{label('会话')}{_green('续连')}  {_dim(f'· {n_messages} 条历史消息')}")
    else:
        rows.append(f"{label('会话')}新对话  {_dim(f'· {run_id}')}")
    rows.append(
        f"{label('项目')}{project_id or _dim('（临时，不持久化）')}"
        + (f"  {_dim('· memory + KB 跨 run 持久化')}" if project_id else "")
    )
    rows.append(f"{label('模型')}{model}")
    if dreaming_line:
        rows.append(f"{label('记忆')}{dreaming_line}")
    print(draw_box(rows, border_style=_dim))
    print(_dim("   /help 命令  ·  /exit 退出") + "\n")


def _print_end_banner(status: str, detail: str | None = None) -> None:
    """会话结束 banner —— 正常/中断/异常三态都给醒目提示（issue #132）。

    status ∈ {'normal', 'interrupted', 'crashed'}。跟启动 banner 同款圆角框，
    让"程序到底是正常退出、被 Ctrl-C 打断、还是崩了"一眼可辨，不再是静默结束。
    """
    if status == "normal":
        head = f"{_green('✅')}  {_bold('会话正常结束')}"
        tip = "下次带同一 --project 可续连（memory + KB 保留）。"
        style = _green
    elif status == "interrupted":
        head = f"{_amber('🛑')}  {_bold('会话已中断')}  {_dim('· Ctrl-C 强制退出')}"
        tip = "本轮可能未跑完；已尽力保存对话。重启带同一 --project 可续连。"
        style = _amber
    else:  # crashed
        head = f"{_c('38;5;203', '❌')}  {_bold('会话异常退出')}"
        tip = "详细堆栈见日志文件（--verbose 可在终端看全量日志）。"
        style = lambda s: _c("38;5;203", s)  # noqa: E731
    rows = [head, ""]
    if detail:
        rows.append(_dim(detail[:200]))
    rows.append(_dim(tip))
    print("\n" + draw_box(rows, border_style=style))


_USER_PROMPT = _cyan(_bold("你")) + _dim(" › ")


_ANSI_RE = re.compile(r"(\033\[[0-9;]*m)")


_USER_PROMPT_RL = _ANSI_RE.sub("\001\\1\002", _USER_PROMPT) if _USE_COLOR else _USER_PROMPT


_STREAM_STATE = {"active": False}


_PROMPT_LIVE = {"on": False}


_IS_TTY = sys.stdout.isatty()


def _norm_text(s: str) -> str:
    """折叠空白，用于判断"回复是否已流式显示过"（流式与最终文本可能只差空白）。"""
    return re.sub(r"\s+", "", s or "")


def _clear_input_line() -> None:
    if _IS_TTY:
        sys.stdout.write("\r\033[K")


def _redraw_prompt() -> None:
    # 写 display 版提示符（真 ANSI）——用户按键时 readline 用 _USER_PROMPT_RL
    # 在同一行重画，二者渲染一致，无缝。非 tty 不画（无交互输入行）。
    if _IS_TTY:
        sys.stdout.write(_USER_PROMPT)
        sys.stdout.flush()


def _emit_above_prompt(text: str) -> None:
    """在活动提示符上方打印一段（整段/多行），然后把提示符重画到下方。

    提示符尚未由线程接管时（启动阶段）或非 tty 时退化成普通 print。

    非 tty 必须 flush=True：管道 / tee / `chat.py | cat` 下 stdout 是块缓冲的，
    不 flush 的话进度提示会全堆在缓冲区里，等一轮跑完才一次性吐出来 —— 用户看到
    的就是"说完话完全没反馈"（issue #166）。"""
    line = text.rstrip("\n")
    if not (_PROMPT_LIVE["on"] and _IS_TTY):
        print(line, flush=True)
        return
    _clear_input_line()
    sys.stdout.write(line + "\n")
    _redraw_prompt()


class _PromptSafeStderr:
    """包一层 sys.stderr：拦截任何直接写 stderr 的裸 print（比如 node hook 自己
    的进度上报，见 nodes/experiment/hooks.py::_emit_experiment_progress），让它们
    也走"清掉输入行 → 写 → 重画提示符"这套协议，不再跟底部 `你 ›` 提示符打架。

    子节点 run 是同进程内的 asyncio task，跟主 REPL 共享同一个终端 —— 任何裸写
    都会在提示符正显示的时候插进来，如果不清行/重画就直接写，视觉上会跟 readline
    自己的重绘互相打断，看起来像提示符被"顶"了好几份（2026-07 实测 bug）。

    按行缓冲：print() 内部可能分多次 write（消息 + 换行符），只在凑齐整行时才
    清行/写/重画，避免半行触发一次无意义的重绘。
    """

    def __init__(self, real):
        self._real = real
        self._buf = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        if not (_PROMPT_LIVE["on"] and _IS_TTY):
            return self._real.write(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            _clear_input_line()
            self._real.write(line + "\n")
            _redraw_prompt()
        return len(s)

    def flush(self) -> None:
        self._real.flush()

    def isatty(self) -> bool:
        return self._real.isatty()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _stream_write(token: str, *, header: str | None = None) -> None:
    """流式逐 token 写：首个 token 先接管输入行（清掉提示符）+ 打 header，
    之后 token 直接顺着写（自然成为 scrollback），提示符期间不在底部。"""
    if not _STREAM_STATE["active"]:
        _clear_input_line()
        if header:
            sys.stdout.write(header + "\n")
        _STREAM_STATE["active"] = True
    sys.stdout.write(token)
    sys.stdout.flush()


def _stream_end() -> None:
    """一段流式输出收尾：换行 + 把提示符重画回底部。幂等。"""
    if _STREAM_STATE["active"]:
        sys.stdout.write("\n")
        _STREAM_STATE["active"] = False
        _redraw_prompt()
        sys.stdout.flush()  # 非 tty 下 _redraw_prompt 是 no-op，这里补 flush


_REASON_MODE = (os.getenv("CHAT_REASONING_DISPLAY") or "brief").strip().lower()


_REASON_TICK_S = 2.0


_REASON_STATE = {"active": False, "chars": 0, "t0": 0.0, "last_tick": 0.0}


def _raw_write(s: str) -> None:
    """直接写 stdout 并 flush（流式/心跳都要即时可见，含非 tty）。"""
    sys.stdout.write(s)
    sys.stdout.flush()


def _reasoning_display(delta: str | None) -> None:
    """LLMClient.stream_reasoning_display 回调：delta=增量，None=推理段结束。

    收尾时补换行 + 重画提示符 —— 正文流随后会自己清行接管，两者不会挤在一行。
    """
    if _REASON_MODE == "off":
        return
    now = time.monotonic()

    if delta is None:
        if not _REASON_STATE["active"]:
            return
        _REASON_STATE["active"] = False
        if _REASON_MODE == "full":
            _raw_write("\n")
        else:
            _raw_write(
                _dim(f" （思考 {_REASON_STATE['chars']} 字，{now - _REASON_STATE['t0']:.0f}s）")
                + "\n"
            )
        _redraw_prompt()
        sys.stdout.flush()
        return

    if not _REASON_STATE["active"]:
        _REASON_STATE.update({"active": True, "chars": 0, "t0": now, "last_tick": now})
        _clear_input_line()  # 接管底部输入行（非 tty 下是 no-op）
        _raw_write("\n" + _dim("💭 思考中" if _REASON_MODE != "full" else "💭 思考：\n"))

    _REASON_STATE["chars"] += len(delta)
    if _REASON_MODE == "full":
        _raw_write(_dim(delta))
    elif now - _REASON_STATE["last_tick"] >= _REASON_TICK_S:
        _REASON_STATE["last_tick"] = now
        _raw_write(_dim("·"))


def _orchestrator_header() -> str:
    return f"{_cyan('🔬')} {_bold(_cyan('Orchestrator'))}"


def _print_reply(reply: str) -> None:
    """orchestrator 回复：带 speaker 标签，在提示符上方打印。

    若本轮已经流式逐 token 显示过（_STREAM_STATE 说明有活动流）则不重复——
    调用方（主循环）负责判断 reply 是否已流式呈现。"""
    _emit_above_prompt(f"\n{_orchestrator_header()}\n{runtime._sanitize_reply(reply)}\n")


def _print_speaker(who: str, text: str) -> None:
    """其它系统侧发言（子节点 / 中途打断决策等）统一带标签打印。"""
    _emit_above_prompt(f"\n{_dim('▸')} {_dim(who)}  {runtime._sanitize_reply(text)}\n")


_PROGRESS_ARG_KEYS = (
    "node_type",
    "query",
    "cmd",
    "artifact_id",
    "mode",
    "artifact_type",
    "name",
    "question",
)


def _describe_call(tool_name: str, args: dict) -> str:
    """从工具名 + 关键参数拼一句人类可读的进度描述。"""
    tool_name = canonical_tool_name(tool_name)
    for k in _PROGRESS_ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            snippet = v.strip().replace("\n", " ")
            if k == "node_type":
                snippet = canonical_node_type(snippet)
            elif k == "artifact_type":
                snippet = canonical_artifact_type(snippet)
            if len(snippet) > 60:
                snippet = snippet[:60] + "…"
            return f"{tool_name}({k}={snippet})"
    return tool_name


def _run_node_targets(tool_name: str, args: dict) -> str:
    """从 run_node / run_nodes_parallel 参数里拿要起的子节点类型（给 header 用）。"""
    if tool_name == "run_nodes_parallel":
        jobs = args.get("jobs")
        if isinstance(jobs, list):
            names = [j.get("node_type") for j in jobs if isinstance(j, dict) and j.get("node_type")]
            if names:
                return " + ".join(canonical_node_type(name) for name in names)
    nt = args.get("node_type")
    return canonical_node_type(nt) if isinstance(nt, str) and nt.strip() else "?"


def _print_progress(state, tool_name: str, args: dict) -> None:
    """agent_loop 进度回调：把"谁在干活"标清楚。

    - orchestrator 自身调 run_node/run_nodes_parallel → 醒目「起子节点」header
    - orchestrator 自身其它工具 → 平齐一行
    - 子节点（depth≥1）工具调用 → 明确标 `[子节点·<type>]` + 按 depth 缩进，
      让用户一眼看出这是子节点在跑，不是主对话
    """
    node = canonical_node_type(getattr(state, "node_type", "?") or "?")
    depth = getattr(state, "depth", 0) or 0

    # 流式回复正在逐 token 打印时，工具调用几乎不会同时发生（一条 LLM 响应要么
    # 出正文要么出 tool_calls）；万一有，先收束流再打进度，避免串行到同一行。
    _stream_end()

    if node == "_orchestrator" and depth == 0:
        if tool_name in ("run_node", "run_nodes_parallel"):
            tgt = _run_node_targets(tool_name, args)
            _emit_above_prompt(
                _dim("   ▸ ") + _cyan(f"起子节点 {tgt}") + _dim(" …（下面是它的工作）")
            )
        else:
            _emit_above_prompt(_dim(f"   ▸ {_describe_call(tool_name, args)}"))
        return

    # 子节点工作：缩进 + 明确标签
    indent = "  " * max(depth, 1)
    _emit_above_prompt(_dim(f"   {indent}└ [子节点·{node}] {_describe_call(tool_name, args)}"))


def _print_pause_prompt(pe) -> None:
    """打印 pause question 给 user（不读 stdin —— 答复走 queue）。"""
    asking = canonical_node_type(pe.asking_node_type or "?")
    lines = ["", "=" * 60, f"[需要人工输入]（来自节点：{asking}）", f"问题：{pe.question}"]
    if pe.context:
        lines.append(f"\n背景：\n{pe.context}")
    if pe.options:
        lines.append("\n选项：")
        for i, opt in enumerate(pe.options, 1):
            lines.append(f"  [{i}] {opt}")
        lines.append("  （或自由输入任意文本）")
    lines.append("=" * 60)
    lines.append("（直接输入答复，会路由给暂停的节点）")
    _emit_above_prompt("\n".join(lines))
