"""Slash command grammar and dispatch to framework-owned operations."""

from __future__ import annotations

import chat as runtime
from core.llm import LLMMessage
from core.state import State

from . import terminal

HELP = """\
可用命令：
  /exit / /quit            退出（自动保存对话）
  /stop                    🛑 停止当前一轮（orchestrator 本轮 + 所有运行中子节点）
  /status                  一次性 dump 项目状态
  /reset                   清空对话历史（memory + KB 保留）
  /undo                    撤销最近一次 artifact 覆盖（覆盖前那版写回为新一版）
  /attach <path>           把本地文件交进项目工作区 sources/（模型每轮自动看到）
  /answer <text>           答复正在等输入的后台子节点（后台 run pause 时用）
  /btw <text>              软注入：作为 system message 插到下一轮 LLM 之前
  /autonomy <档>           切档位：assisted（需要用户判断时再问）| autonomous（只在高危点停）
                           | continuous（预授权全部高危类别，不停）。与 UI 三档同一份词表。
  /continuous on|off       切**自动续轮**（跑完一轮自己接着跑）；on 会把档位一并提到 continuous
  /skip-dreaming           跳过本次 dreaming pending
  /help                    显示这段说明

直接输入文字 → 走 orchestrator agent loop。
多行粘贴：整段粘进来会作为**一条**消息，末尾按一次 Enter 才提交（内部换行保留）。
child 跑的时候你可以继续输入：
  - 没主动 pause → orchestrator 决策 inject_into_node 或 cancel_node 给 child
  - 已主动 pause（看到 [需要人工输入] 或 decision package 提示）→ 输入直接当 pause 答复
  - /stop → 全部停下
  - /autonomy /continuous /status /btw /attach /answer /skip-dreaming /help
    跑轮中也能用（child 连环撞高危确认时切 /autonomy continuous 就不用等）；
    /reset /undo 会动运行中状态，只能空闲时用

启动 flag：--continuous 等价于 /autonomy continuous + /continuous on —— 档位提到「连续」
             （⚠️ 预授权全部高危类别，高危命令不再问人）并持续自动续轮，
             只有明确 complete / 真正 human-only blocked / user /stop 才停。
"""


_EXIT_COMMANDS = ("/exit", "/quit")


def _is_slash_command(text: str) -> bool:
    return text.startswith("/")


_MIDTURN_SAFE_SLASH = (
    "/autonomy",
    "/continuous",
    "/status",
    "/help",
    "/btw",
    "/attach",
    "/answer",
    "/skip-dreaming",
)


def _midturn_slash_allowed(text: str) -> bool:
    return any(text == c or text.startswith(c + " ") for c in _MIDTURN_SAFE_SLASH)


async def _handle_slash(text: str, state: State, messages: list[LLMMessage]) -> bool:
    """处理 / 命令。返 True 表示 user 想退出，False 表示已处理。"""
    if text in _EXIT_COMMANDS:
        return True
    if text == "/help":
        terminal._emit_above_prompt(HELP)
    elif text == "/status":
        terminal._emit_above_prompt(await runtime._cmd_status(state))
    elif text in ("/stop", "/abort"):
        if runtime._continuous_loop(state):
            runtime._set_continuous_loop(state, False)
            terminal._emit_above_prompt("（当前轮次空闲；自动续轮已关闭。自主档位不变。）")
        else:
            terminal._emit_above_prompt(
                "（当前没有正在跑的轮次 —— /stop 在 turn 进行中才有作用。）"
            )
    elif text == "/undo":
        terminal._emit_above_prompt(runtime._cmd_undo(state))
    elif text.startswith("/attach "):
        terminal._emit_above_prompt(runtime._cmd_attach(state, text[len("/attach ") :].strip()))
    elif text.startswith("/answer "):
        terminal._emit_above_prompt(await runtime._cmd_answer(text[len("/answer ") :].strip()))
    elif text == "/reset":
        terminal._emit_above_prompt(runtime._cmd_reset(state, messages))
        runtime._save_conversation(state, messages)
    elif text.startswith("/btw "):
        payload = text[5:].strip()
        if payload:
            queue = state.hook_state.setdefault("pending_btw_injection", "")
            state.hook_state["pending_btw_injection"] = queue + ("\n" if queue else "") + payload
            terminal._emit_above_prompt(
                f"（已登记 /btw 注入：{payload[:80]}… —— 下次 LLM 调用前注入）"
            )
    elif text == "/skip-dreaming":
        # Phase F：清当前 project 的 dreaming pending，不让本会话自动跑
        if state.project_id:
            try:
                from core.dreaming_scheduler import clear_pending

                clear_pending(state.project_id)
                terminal._emit_above_prompt(
                    "（已跳过本次 dreaming pending；下次触发条件满足时再 mark）"
                )
            except Exception as e:
                terminal._emit_above_prompt(f"（清 dreaming pending 失败：{e}）")
        else:
            terminal._emit_above_prompt("（无 project_id —— 没有 dreaming pending 可跳过）")
    elif text.startswith("/autonomy"):
        # /autonomy assisted|autonomous|continuous —— 与 UI 三档同一份词表。
        #
        # 这条命令取代了 `/auto_approve` 与 `/bypass`：那两条各自设一个进程全局
        # 开关，然后各自"顺手"把 continuous 也关掉，理由写在提示语里
        # （"因为它要求 auto-approve"）。三条命令写三个投影，于是"档位现在是
        # 什么"要把三个全局读齐了才答得出，而它们能互相矛盾。现在只有一份声明，
        # 三个开关都是它推导出来的（`session_driver.apply_autonomy`）。
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            terminal._emit_above_prompt(
                f"（当前档位 = {runtime._autonomy_label(state)}；"
                "用法：/autonomy assisted|autonomous|continuous）"
            )
        else:
            arg = parts[1].strip().lower()
            if arg not in runtime.AUTONOMY_SCOPES:
                terminal._emit_above_prompt(
                    f"（无效档位 {arg!r}；合法取值：{', '.join(runtime.AUTONOMY_SCOPES)}）"
                )
            else:
                runtime._set_autonomy_mode(state, arg)
                terminal._emit_above_prompt(f"（档位 → {runtime._autonomy_label(state)}）")
    elif text.startswith("/continuous"):
        # /continuous on|off —— 只管**自动续轮**。档位归 /autonomy。
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            phase = state.hook_state.get("continuous_phase", "stopped")
            extra = ""
            if phase == "aborted":
                extra = "；🛑 已被熔断停机：" + str(
                    state.hook_state.get("continuous_abort_reason", "")
                )
            terminal._emit_above_prompt(
                f"（自动续轮 = {'ON' if runtime._continuous_loop(state) else 'OFF'}"
                f"，phase={phase}{extra}；档位 = {runtime._autonomy_label(state)}；"
                "用法：/continuous on|off）"
            )
        else:
            arg = parts[1].strip().lower()
            if arg in ("on", "true", "1", "yes"):
                # 在自己终端里说"一路跑到底"，意思是两件事都要 —— 但要说出来，
                # 不能让一条命令悄悄改掉另一件事。
                runtime._set_autonomy_mode(state, "continuous")
                runtime._set_continuous_loop(state, True)
                state.hook_state["continuous_kick_requested"] = True
                terminal._emit_above_prompt(
                    "（continuous ON ♾️ —— 自动续轮开启，档位同时提到「连续」"
                    "（预授权全部高危类别）。顶层回合会自动续接，直到 "
                    "complete / human-only blocked / /stop。）"
                )
            elif arg in ("off", "false", "0", "no"):
                runtime._set_continuous_loop(state, False)
                terminal._emit_above_prompt(
                    "（continuous OFF —— 自动续轮停止；档位不变，"
                    f"仍是 {runtime._autonomy_label(state)}。要改档位用 /autonomy。）"
                )
            else:
                terminal._emit_above_prompt(f"（无效参数 {arg!r}；用 on|off）")
    else:
        terminal._emit_above_prompt(f"（未知命令：{text}。输入 /help 查看可用命令。）")
    return False
