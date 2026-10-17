"""v3 的两个 loop hook（governing document §5、§7）。

* ``experiment_ledger_recorder``（on_turn_end）：共同记账边界的兜底——本轮所有
  有副作用的工具调用（写文件、取资源、存 / 冻 artifact、跑命令、提作业……）都
  以 ``effect`` 记录进账本；工具内已经记过的（action / intent / observation）
  这里只补一条轻量 effect 索引，不重复权威。
* ``experiment_delta_view``（on_turn_end）：有上限的事实视图——开放作业、
  obligations 每项当前在不在、最近一次失败信号、账本写入失败。**只有事实，
  没有指令**；S1d 起才进 harness 的 loop_hooks，替换旧注入。

两个 hook 都不设门：账本写不进去只披露，不拒绝任何事。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.llm import LLMMessage
from core.loop_hooks import HookContext, LoopHook, get_loop_hook, register_loop_hook

try:
    from . import ledger as _ledger
    from . import obligations as _obligations
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools import ledger as _ledger  # type: ignore
    from tools import obligations as _obligations  # type: ignore

#: 有副作用的模型面工具及其 effect 分类。
EFFECTFUL_TOOLS: dict[str, str] = {
    "safe_run_bash": "experiment", "safe_execute_python": "experiment", "submit_job": "experiment",
    "cancel_job": "experiment", "safe_write_file": "support", "fetch_resource": "support",
    "save_artifact": "support", "freeze_artifact": "support", "dispatch_data_request": "support",
    "finish": "closure",
}
_VIEW_MAX_CHARS = 1800


def _summarize_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "safe_run_bash": ("cmd", "cwd", "timeout"), "safe_execute_python": ("cwd", "timeout"),
        "submit_job": ("scheduler", "job_name", "workdir", "output_paths"), "cancel_job": ("scheduler", "job_id"),
        "safe_write_file": ("path",), "fetch_resource": ("url", "destination", "kind"),
        "save_artifact": ("artifact_type", "name"), "freeze_artifact": ("artifact_id",),
        "dispatch_data_request": ("spec_id",), "finish": ("outcome",),
    }.get(name, ())
    out: dict[str, Any] = {}
    for key in keep:
        if key in args:
            value = args[key]
            out[key] = value[:300] if isinstance(value, str) else value
    return out


def _summarize_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "unknown"}
    out: dict[str, Any] = {"status": result.get("status")}
    for key in ("returncode", "job_id", "scheduler", "written_path", "destination", "sha256", "size_bytes",
                "artifact_id", "id", "error_code", "outcome", "closure_id"):
        if key in result:
            out[key] = result[key]
    return out


def record_turn_effects(ctx: HookContext) -> int:
    """把本轮的有副作用调用记成 effect；返回记了几条。"""
    records = getattr(ctx, "tool_call_records", None) or []
    count = 0
    for row in records:
        name = str(row.get("name") or "")
        klass = EFFECTFUL_TOOLS.get(name)
        if not klass:
            continue
        args = row.get("args") if isinstance(row.get("args"), dict) else {}
        summary = _summarize_result(row.get("result"))
        if _ledger.try_record(ctx.state, "effect", tool=name, effect_class=klass, turn=ctx.turn,
                              args=_summarize_args(name, args), result=summary) is not None:
            count += 1
    return count


def _on_turn_end_recorder(ctx: HookContext) -> None:
    try:
        record_turn_effects(ctx)
    except Exception:  # 记账兜底自己不能把 run 弄死
        return None
    return None


def build_delta_view(state: Any) -> str:
    """事实视图正文（无上限时的完整版；调用方再截断）。"""
    led = _ledger.open_ledger(state)
    try:
        records = led.records()
    except _ledger.LedgerCorrupt as exc:
        return f"账本损坏：{exc}"
    lines: list[str] = []
    if not any(r.get("kind") in {"run_bootstrap", "scope_resolved"} and r.get("scope") for r in records):
        lines.append("scope：未定（保留 unknown；按任务验收与行动证据收尾，不要求补分类手续）")
    open_jobs = _ledger.open_jobs(records)
    if open_jobs:
        lines.append("开放作业：" + "；".join(
            f"{j.get('external_id') or j['nonce'][:8]}（{j['state']}，{j.get('scheduler') or '?'}）" for j in open_jobs[:6]))
    else:
        lines.append("开放作业：无")
    obligations = _obligations.frozen(state)
    if obligations:
        parts = []
        for ob in obligations:
            if ob["kind"] == "artifact":
                continue
            if ob["kind"] == "file":
                p = _obligations.resolve_path(state, ob["path"])
                parts.append(f"{ob['id']}：{'在' if p.is_file() else '不在'}")
            elif ob["kind"] == "glob":
                import glob as _g
                n = len(_g.glob(str(_obligations.resolve_path(state, ob["pattern"])), recursive=True))
                parts.append(f"{ob['id']}：{n} 个")
            elif ob["kind"] == "task":
                text = str(ob["text"])
                excerpt = text[:160] + ("…" if len(text) > 160 else "")
                parts.append(f"原任务要求（全文已保存）：{excerpt}；核验方法与证据由 finish verification 记录")
            else:
                parts.append(f"{ob['id']}：检查命令（finish 时框架跑）")
        if parts:
            lines.append("交付物：" + "；".join(parts))
    else:
        lines.append("任务要求：未冻结；核对是否提供了任务正文或结构化交付要求，缺失时澄清实际任务。")
    last_fail = None
    for row in reversed(records):
        if row.get("kind") == "action_settled" and (row.get("exit_code") not in (0, None) or row.get("fatal_signals")):
            last_fail = row
            break
        if row.get("kind") == "job_terminal" and str(row.get("derived_state") or "").upper() not in {"COMPLETED", "SUCCESS", "SUCCEEDED"}:
            last_fail = row
            break
    if last_fail is not None:
        if last_fail.get("kind") == "job_terminal":
            lines.append(f"最近失败信号：作业 {last_fail.get('external_id') or last_fail.get('nonce')} 终态 {last_fail.get('derived_state')}")
        else:
            sig = last_fail.get("fatal_signals") or last_fail.get("diagnosis") or f"exit {last_fail.get('exit_code')}"
            lines.append(f"最近失败信号：{last_fail.get('action_id')} {sig}")
    failures = (getattr(state, "hook_state", {}) or {}).get("ledger_write_failures") or []
    if failures:
        lines.append(f"账本写入失败 {len(failures)} 次（最近：{failures[-1].get('reason')}）")
    if led.read_only and led.epoch == "v3" and led.owner is not None and "owned by" in str(led.reason):
        lines.append(f"本会话只读：{led.reason}")  # 只在账本归别的活会话时才说；视图自己本来就是读者
    return "\n".join(lines)


def _on_turn_end_delta_view(ctx: HookContext) -> list[LLMMessage] | None:
    """只在 v3 run 上、且视图内容有变化时注入；旧协议 run 与无变化的轮次零字。

    每轮无条件追加一条 system 消息会把工具结果从消息尾部挤开（test_egress_grant_037
    的桩模型只看最后一条），也违反"不强迫模型复述已知事实"。
    """
    state = ctx.state
    try:
        if _ledger.bootstrap_phase(state) != "ready":
            return None
        text = build_delta_view(state)
    except Exception as exc:
        text = f"视图不可用：{type(exc).__name__}: {exc}"
    if len(text) > _VIEW_MAX_CHARS:
        text = text[:_VIEW_MAX_CHARS] + "\n…（视图按上限截断）"
    hook_state = getattr(state, "hook_state", None)
    if isinstance(hook_state, dict):
        if hook_state.get("v3_delta_view_last") == text:
            return None
        hook_state["v3_delta_view_last"] = text
    return [LLMMessage(role="system", content="[状态视图]\n" + text)]


def _register_once(hook: LoopHook) -> None:
    """同一个模块可能以两个模块名各 import 一次；注册一次即可，不刷覆盖警告。"""
    if get_loop_hook(hook.name) is None:
        register_loop_hook(hook)

experiment_ledger_recorder = LoopHook(
    name="experiment_ledger_recorder",
    description="v3 共同记账边界的兜底：本轮所有有副作用的工具调用以 effect 记入账本（只记录，不判断）",
    on_turn_end=_on_turn_end_recorder,
)
_register_once(experiment_ledger_recorder)

def _on_end_live_effect_unsettled(ctx: HookContext, loop_result: Any) -> None:
    """run 结束时还有开放 / unknown 作业：把这个事实持久化进账本（Codex 36 P0-E）。

    只记事实，不假装挡住文本终止——挡住模型直接写最终回答是 Core 的终止边界（handoff）。
    """
    state = ctx.state
    try:
        if _ledger.bootstrap_phase(state) != "ready":
            return None
        led = _ledger.open_ledger(state)
        open_jobs = _ledger.open_jobs(led.records())
        if not open_jobs:
            return None
        sealed = any(r.get("kind") == "finish" for r in led.records())
        _ledger.try_record(state, "live_effect_unsettled",
                           jobs=[{"nonce": j.get("nonce"), "external_id": j.get("external_id"),
                                  "state": j.get("state"), "scheduler": j.get("scheduler")} for j in open_jobs[:20]],
                           finish_called=sealed,
                           loop_status=str(getattr(loop_result, "status", "") or ""))
    except Exception:
        return None
    return None


experiment_live_effect_unsettled = LoopHook(
    name="experiment_live_effect_unsettled",
    description="v3 on_end 事实：run 结束时仍有开放/unknown 作业则记 live_effect_unsettled（只记录，不是终止门；终止门归 Core）",
    on_end=_on_end_live_effect_unsettled,
)
_register_once(experiment_live_effect_unsettled)

experiment_delta_view = LoopHook(
    name="experiment_delta_view",
    description="v3 事实视图：开放作业、交付物在不在、最近失败信号、账本写入失败；只有事实，没有指令（S1d 起启用）",
    on_turn_end=_on_turn_end_delta_view,
)
_register_once(experiment_delta_view)
