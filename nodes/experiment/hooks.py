"""
experiment 节点自定义 hooks —— 通用错误检测框架 v2

设计原则：
- 不硬编码特定工具的错误模式
- 使用 diagnose_engine 进行通用错误检测
- 支持任意工具的日志分析

4 个钩子点：
  - on_turn_start:  每轮 LLM 调用前（可 inject 消息）
  - on_llm_response: LLM 返回后、工具调度前（只观察）
  - on_turn_end:    工具调度后、下轮 LLM 前（可 inject 消息）
  - on_end:         loop 终止后（只观察）

双注册机制（兼容框架 + 测试）：
  1. 模块顶层调用 register_loop_hook() → 框架自动加载
  2. 同时 export 函数引用作为顶层符号 → 测试可直接 import
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core.loop_hooks import HookContext, LoopHook, register_loop_hook
from core.llm import LLMMessage, LLMResponse

# 导入通用诊断引擎
sys.path.insert(0, str(Path(__file__).parent))
from tools.diagnose import (
    DiagnoseEngine, focus_error, fault_fingerprint, is_hard_repeat_eligible,
)
from tools.path_roles import collect_path_roles, experiment_output_dir
try:
    from . import stuck_signals
except ImportError:  # hooks.py is also loaded directly by compatibility tests
    import stuck_signals
import tools.contract_audit as _contract_audit
from tools.contract_audit import (
    audit_experiment_contract,
    audit_operation_log_contract,
    current_run_artifacts,
    terminal_closure_projection,
)
from tools.operation_completion import operation_child_obligation_projection
from tools.run_contract import audit_prereg_assignment
try:
    from .task_prose_inputs import (
        TASK_PROSE_INPUT_RECEIPT_EVENT,
        first_task_prose_source,
        freeze_initial_task_prose_source,
    )
except ImportError:  # hooks.py can also be loaded directly by compatibility tests.
    from task_prose_inputs import (
        TASK_PROSE_INPUT_RECEIPT_EVENT,
        first_task_prose_source,
        freeze_initial_task_prose_source,
    )


def _sys(content: str) -> LLMMessage:
    """快捷构造 system 角色的 LLMMessage，供所有 hooks 使用。"""
    return LLMMessage(role="system", content=content)


def _is_operational_run(state: Any) -> bool:
    """Whether Experiment classified the request as a non-scientific operation."""
    try:
        from tools.run_contract import load_run_contract
        return load_run_contract(state).get("execution_mode") == "operational"
    except Exception:
        return False

def _audit_operation_log(state: Any) -> dict[str, Any]:
    """Compatibility wrapper around the sole operation receipt audit."""
    return audit_operation_log_contract(state)


def _operation_outcome_receipt(
    state: Any,
    audit: dict[str, Any],
) -> dict[str, str | None] | None:
    """Read the effective outcome from the canonical frozen closure.

    ``operation_closure_input.outcome`` is written only after the completion
    tool has applied its mechanical success-to-partial demotion.  The
    requested value and demotion source remain audit context; they must never
    override that effective value when deciding what Core may summarize.
    """
    artifact_id = str(audit.get("artifact_id") or "")
    if not artifact_id:
        return None
    try:
        record = state.read_artifact(artifact_id)
    except Exception:
        return None
    metadata = (record or {}).get("metadata") or {}
    closure_input = (
        metadata.get("operation_closure_input")
        if isinstance(metadata, dict)
        else None
    )
    if not isinstance(closure_input, dict):
        return None
    effective = str(closure_input.get("outcome") or "").strip().lower()
    if not effective:
        return None
    requested = str(
        closure_input.get("requested_outcome", effective) or effective
    ).strip().lower()
    demoted_from = str(closure_input.get("outcome_demoted_from") or "").strip().lower()
    return {
        "effective_outcome": effective,
        "requested_outcome": requested,
        "outcome_demoted_from": demoted_from or None,
    }


def _append_operation_child_delivery_summary(
    loop_result: Any,
    child_obligation: dict[str, Any],
) -> None:
    """Expose the frozen child delivery without deciding the parent goal."""
    marker = "## Experiment Child Delivery"
    current = str(getattr(loop_result, "final_text", "") or "")
    if marker in current:
        return
    # P0a v3 M5（Codex 复审 17 号）：delivery_status 是**冻结收尾**的状态，不是 run 的
    # 终态。loop 以 max_turns / failed 收尾、或后面某道审计把 run 判 blocked 时，
    # 页脚不能只留一句 completed。这里写明 run 终态；之后再被 block 的，由
    # _block_experiment_completion 追加更正行。
    run_status = str(getattr(loop_result, "status", "") or "unknown")
    footer = (
        f"\n\n{marker}\n"
        f"- closure_id: {child_obligation.get('closure_id')}\n"
        f"- delivery_status: {child_obligation.get('delivery_status')}\n"
        f"- upstream_goal_effect: {child_obligation.get('upstream_goal_effect')}\n"
        f"- scientific_contribution: {child_obligation.get('scientific_contribution')}\n"
        "- parent_goal_completion_claimed: false\n"
        f"- run_terminal_status: {run_status}"
        + ("" if run_status in {"completed", "success"} else
           "\n- note: delivery_status describes the frozen closure; the run itself did "
           "not end completed")
    )
    try:
        loop_result.final_text = current + footer
    except Exception:
        pass


def _annotate_child_delivery_footer_blocked(loop_result: Any, blocker_id: str) -> None:
    """If a child-delivery footer was already written, record that the run was
    blocked afterwards, so the footer never ends on a stale 'completed'."""
    marker = "## Experiment Child Delivery"
    current = str(getattr(loop_result, "final_text", "") or "")
    if marker not in current or f"blocked_after_delivery: {blocker_id}" in current:
        return
    try:
        loop_result.final_text = (
            current + f"\n- run_terminal_status: blocked\n- blocked_after_delivery: {blocker_id}"
        )
    except Exception:
        pass


def _block_experiment_completion(
    state: Any,
    loop_result: Any,
    *,
    blocker_id: str,
    reason: str,
    failed_checks: list[str],
    summary: str,
) -> None:
    """Make a node-local closure failure visible to Core finalization.

    finalize_run derives durable status from hook_state["blockers"], rather
    than from an on-end hook"s ephemeral loop_result.status. Therefore an
    audit failure must write both the machine-readable downstream state and a
    durable blocker; otherwise a Workspace run can be summarized as completed
    after a failed audit.
    """
    checks = sorted({str(item) for item in failed_checks if str(item)})
    state.hook_state["experiment_downstream_blocked"] = {
        "reason": reason,
        "failed_checks": checks,
        "review_eligibility": False,
    }
    _annotate_child_delivery_footer_blocked(loop_result, blocker_id)
    blockers = state.hook_state.setdefault("blockers", [])
    if not any(
        item.get("blocker_id") == blocker_id
        for item in blockers
        if isinstance(item, dict)
    ):
        blockers.append({
            "blocker_id": blocker_id,
            "category": "closure",
            "summary": summary,
            "retryable_after_change": True,
        })
    # The blocker above is the status authority. Transcript persistence is
    # valuable audit evidence, but must never be able to undo that fail-closed
    # state by raising from an on_end hook.
    try:
        state.append_transcript(
            "experiment_downstream_blocked",
            reason=reason,
            failed_checks=checks,
            review_eligibility=False,
        )
    except Exception:
        log.warning("unable to record experiment completion blocker", exc_info=True)
    if str(getattr(loop_result, "status", "") or "") not in {"cancelled", "paused"}:
        try:
            loop_result.status = "blocked"
        except Exception:
            pass


def _append_mechanical_failure_footer(
    loop_result: Any,
    checks: dict[str, str],
) -> None:
    """Expose an enforced failure in the user-facing completion text once."""
    current = str(getattr(loop_result, "final_text", "") or "")
    if "## Framework Mechanical Status" in current:
        return
    lines = ["", "", "## Framework Mechanical Status", "- overall_status: blocked"]
    for name, detail in checks.items():
        lines.append(f"- {name}: failed")
        lines.append(f"  reason: {detail}")
    lines.append("- note: the model narrative is not an authoritative completion status.")
    try:
        loop_result.final_text = current + "\n".join(lines)
    except Exception:
        log.warning("无法将机械审计失败写入 final_text")


log = logging.getLogger("experiment_hooks")

_PROGRESS_FALSE_VALUES = {"0", "false", "off", "no", "quiet"}


def _experiment_progress_enabled() -> bool:
    value = os.getenv("EXPERIMENT_NODE_PROGRESS", "1").strip().lower()
    return value not in _PROGRESS_FALSE_VALUES


def _compact_progress_value(value: Any, *, max_len: int = 160) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.replace("\n", "\\n")
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


# 参数摘要的 key 优先级：命令/代码 > 交互问题 > artifact 定位 > 文件路径 > 其它定位符
_PROGRESS_ARG_PRIORITY = (
    "command", "cmd", "code", "script",
    "question", "prompt", "query",
    "artifact_id", "chunk_id", "artifact_type", "type", "name",
    "path", "file_path", "filename", "target", "url", "pattern",
    "content",
)
_PROGRESS_MAX_ARG_KEYS = 3

_PROGRESS_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|passw(or)?d|credential|authorization)", re.IGNORECASE
)
# 文本内嵌的 KEY=value / KEY: value / Bearer xxx —— 只留 key，值打码
_PROGRESS_SECRET_VALUE_RE = re.compile(
    r"((?:api[_-]?key|access[_-]?key|token|secret|passw(?:or)?d|authorization)"
    r"[a-z0-9_]*\s*[=:]\s*)(\"[^\"]+\"|'[^']+'|bearer\s+\S+|\S+)",
    re.IGNORECASE,
)
_PROGRESS_BEARER_RE = re.compile(r"(bearer\s+)\S+", re.IGNORECASE)


def _redact_progress_text(text: str) -> str:
    """stderr/transcript 心跳脱敏：API key / token / password 的值一律打码。"""
    text = _PROGRESS_SECRET_VALUE_RE.sub(r"\1***", text)
    return _PROGRESS_BEARER_RE.sub(r"\1***", text)


def _summarize_tool_args(args: Any, *, max_len_per_value: int = 80) -> str:
    """把 tool call 参数压成一行 key=\"value\" 摘要（截断 + 脱敏）。"""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return _redact_progress_text(_compact_progress_value(args, max_len=max_len_per_value))
    if not isinstance(args, dict) or not args:
        return ""
    picked = [key for key in _PROGRESS_ARG_PRIORITY if args.get(key) not in (None, "", [], {})]
    if not picked:  # 全是优先级表外的 key → 按原顺序兜底取前几个
        picked = [key for key, value in args.items() if value not in (None, "", [], {})]
    parts = []
    for key in picked[:_PROGRESS_MAX_ARG_KEYS]:
        if _PROGRESS_SECRET_KEY_RE.search(key):
            parts.append(f'{key}="***"')
            continue
        value = _compact_progress_value(args[key], max_len=max_len_per_value)
        parts.append(f'{key}="{_redact_progress_text(value)}"')
    return " ".join(parts)


def _tool_result_status(result: Any) -> str:
    if isinstance(result, dict):
        status = result.get("status")
        if status:
            return str(status)
        if result.get("error"):
            return "error"
        return "ok"
    return type(result).__name__


def _emit_experiment_progress(
    state: Any,
    stage: str,
    detail: str = "",
    *,
    terminal: bool = True,
    **fields: Any,
) -> None:
    """Emit a concise live heartbeat for experiment runs.

    Experiment jobs can run for many turns while compiling or debugging HPC
    software. This mirrors data-node progress output while keeping the event
    in transcript for later inspection. Set EXPERIMENT_NODE_PROGRESS=0 to
    silence terminal output; transcript events are still recorded.
    """
    payload = {
        "stage": stage,
        "detail": detail,
        **{key: value for key, value in fields.items() if value is not None},
    }
    try:
        state.append_transcript("experiment_progress", **payload)
    except Exception:
        pass
    # ``tool_done`` duplicates the just-printed ``tool_plan`` arguments. Keep
    # it in the durable transcript for runtime-control/audit consumers, while
    # omitting it from the live terminal stream.
    if not terminal or not _experiment_progress_enabled():
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    extras = " ".join(
        f"{key}={_compact_progress_value(value)}"
        for key, value in payload.items()
        if key not in {"stage", "detail"} and value not in ("", None, [], {})
    )
    message = f"[experiment {timestamp}] {stage}"
    if detail:
        message += f": {detail}"
    if extras:
        message += f" ({extras})"
    print(message, file=sys.stderr, flush=True)


def experiment_progress_on_turn_start(ctx: HookContext) -> None:
    max_turns = ctx.harness.max_turns
    turn_limit = "∞" if max_turns <= 0 else str(max_turns)
    _emit_experiment_progress(
        ctx.state,
        "turn_start",
        f"turn {ctx.turn}/{turn_limit}",
        run_id=ctx.state.run_id,
    )


def experiment_progress_on_llm_response(
    ctx: HookContext,
    response: LLMResponse,
) -> None:
    calls = response.tool_calls or []
    preview = (response.content or "").strip().replace("\n", " ")
    if not calls:
        _emit_experiment_progress(
            ctx.state,
            "llm_response",
            _redact_progress_text(_compact_progress_value(preview, max_len=220))
            if preview else "no tool call",
            turn=ctx.turn,
            finish_reason=response.finish_reason,
        )
        return
    tool_names = [
        ((call.get("function") or {}).get("name") or "?") for call in calls
    ]
    # 双轨输出：意图文本（模型 tool-call 前写的说明，可能为空）+ 每个 call 的参数摘要
    detail = ", ".join(tool_names)
    if preview:
        detail += " — " + _redact_progress_text(_compact_progress_value(preview, max_len=160))
    _emit_experiment_progress(
        ctx.state,
        "llm_tool_calls",
        detail,
        turn=ctx.turn,
        finish_reason=response.finish_reason,
        tool_count=len(tool_names),
    )
    for call in calls:
        function = call.get("function") or {}
        name = function.get("name") or "?"
        args_summary = _summarize_tool_args(function.get("arguments"))
        _emit_experiment_progress(
            ctx.state,
            "tool_plan",
            f"{name} [{args_summary}]" if args_summary else name,
            turn=ctx.turn,
        )


def experiment_progress_on_turn_end(ctx: HookContext) -> None:
    records = ctx.tool_call_records or []
    if not records:
        return
    total = int(ctx.state.hook_state.get("experiment_progress_tool_count", 0))
    for record in records:
        total += 1
        result = record.get("result") if isinstance(record, dict) else None
        name = str(record.get("name") or "?")
        args_summary = _summarize_tool_args(record.get("args"))
        returncode = result.get("returncode") if isinstance(result, dict) else None
        _emit_experiment_progress(
            ctx.state,
            "tool_done",
            f"{name} [{args_summary}]" if args_summary else name,
            terminal=False,
            turn=ctx.turn,
            tool_index=total,
            status=_tool_result_status(result),
            returncode=returncode,
        )
    ctx.state.hook_state["experiment_progress_tool_count"] = total


def experiment_progress_on_end(ctx: HookContext, loop_result: Any) -> None:
    _emit_experiment_progress(
        ctx.state,
        "end",
        f"status={getattr(loop_result, 'status', '?')}",
        turns=getattr(loop_result, "turns", ctx.turn),
        tool_calls=ctx.state.hook_state.get("experiment_progress_tool_count", 0),
        run_id=ctx.state.run_id,
    )


experiment_progress_reporter = LoopHook(
    name="experiment_progress_reporter",
    description="experiment live progress: turn start, requested tools, tool completion, and loop end.",
    on_turn_start=experiment_progress_on_turn_start,
    on_llm_response=experiment_progress_on_llm_response,
    on_turn_end=experiment_progress_on_turn_end,
    on_end=experiment_progress_on_end,
)
register_loop_hook(experiment_progress_reporter)

# v0.11 工具改名迁移（同名覆盖清退）：transcript/tool_call 匹配须兼容新旧名 ——
# 新 run 记 safe_run_bash / safe_execute_python，旧 transcript / 旧 fixture 复盘
# 仍是 run_bash / execute_python。
_BASH_TOOL_NAMES = frozenset({"run_bash", "safe_run_bash"})
_PY_TOOL_NAMES = frozenset({"execute_python", "safe_execute_python"})


def _safe_bash():
    """惰性解析 safe_bash 模块（本文件访问它的唯一入口）。

    不能在 hooks 顶层 import：safe_bash 在模块顶层注册工具，若以另一个模块名
    再次加载就会重复注册。因此**先查 sys.modules**（bootstrap 之后它必然已在，
    且可能登记为两种名字之一），只有都没有时才真正 import。
    """
    for name in ("nodes.experiment.tools.safe_bash", "tools.safe_bash"):
        module = sys.modules.get(name)
        if module is not None:
            return module
    import importlib
    for name in ("nodes.experiment.tools.safe_bash", "tools.safe_bash"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ImportError("safe_bash 模块不可用")


def _bench_off(cap: str) -> bool:
    """第6步 benchmark：当前组禁用该能力则 True（hook 顶部守卫用）。
    复用 safe_bash 单一开关（EXPERIMENT_BENCH_GROUP）；未设变量=全开→False，日常零影响。"""
    try:
        return not _safe_bash().bench_enabled(cap)
    except Exception:
        # 取不到开关 = 按"能力全开"处理：这是 benchmark 开关的正常默认，
        # 不是安全门，失败不该反而关掉功能。
        return False


# ─────────────────────────────────────────────
# execution_supervisor —— 中途提示的唯一仲裁点
# ─────────────────────────────────────────────
# 检测器仍负责读取证据、更新计数和终态审计；只有本仲裁器可把中途建议写进
# LLM 上下文。工具层的安全/契约阻断不经过这里，仍会立即生效。

_SUPERVISOR_CANDIDATES_KEY = stuck_signals.LEGACY_PENDING_KEY
_SUPERVISOR_LAST_KEY = stuck_signals.LEGACY_LAST_INJECTED_KEY
_SUPERVISOR_COOLDOWN_TURNS = 5


def _supervisor_offer(ctx: HookContext, *, source: str, priority: int,
                      content: str, dedupe_key: str | None = None,
                      scope: str = "any") -> None:
    """Adapt an existing detector to the shared advisory-signal store."""
    stuck_signals.offer(
        ctx.state.hook_state, source=source, priority=priority, content=content,
        dedupe_key=dedupe_key, scope=scope,
    )

def _supervisor_offer_result(ctx: HookContext, *, source: str, priority: int,
                             result: list | None) -> None:
    """Adapt legacy hook return values into a supervisor candidate."""
    if not result:
        return
    first = result[0] if isinstance(result, list) else result
    content = first.get("content") if isinstance(first, dict) else getattr(first, "content", None)
    if content:
        _supervisor_offer(ctx, source=source, priority=priority, content=str(content))


def execution_supervisor_on_turn_start(ctx: HookContext) -> list | None:
    """Inject at most one highest-priority advisory, with per-issue cooldown.

    The supervisor is advisory-only: malformed bookkeeping must never stop a
    run, and a scientific-only suggestion may never leak into an operation.
    """
    hook_state = ctx.state.hook_state
    try:
        operational = _is_operational_run(ctx.state)
    except Exception:
        operational = False
    selected, suppressed, candidate_count = stuck_signals.choose(
        hook_state, turn=ctx.turn, operational=operational,
        cooldown_turns=_SUPERVISOR_COOLDOWN_TURNS,
    )
    for item in suppressed:
        try:
            ctx.state.append_transcript(
                "execution_supervisor_suppressed", turn=ctx.turn,
                source=item.source, dedupe_key=item.dedupe_key, reason=item.reason,
            )
        except Exception:
            pass
    if selected is None:
        return None
    stuck_signals.mark_injected(hook_state, selected, turn=ctx.turn)
    try:
        ctx.state.append_transcript(
            "execution_supervisor_injected", turn=ctx.turn,
            source=selected.source, priority=selected.priority,
            dedupe_key=selected.dedupe_key, candidates=candidate_count)
    except Exception:
        pass
    return [_sys(selected.content)]

execution_supervisor = LoopHook(
    name="execution_supervisor",
    description="中途提示唯一仲裁：每轮最多注入一条，按故障/重复/无进度/健康优先级选择",
    on_turn_start=execution_supervisor_on_turn_start,
    emits=("execution_supervisor_injected", "execution_supervisor_suppressed"),
)
register_loop_hook(execution_supervisor)


# ─────────────────────────────────────────────
# Generic Failure Detector —— 通用错误检测器 v2
# ─────────────────────────────────────────────

# 常见日志文件模式（按优先级）
LOG_FILE_PATTERNS = [
    # 常见并行程序日志命名
    "rsl.error.0000", "rsl.error.*", "rsl.out.0000", "log.*",
    # 通用
    "*.log", "*.err", "*.out",
    # 特定工具
    "stdout", "stderr", "output.log", "error.log",
]


def _find_log_files(state_dir: str | Path) -> list[Path]:
    """在 state 目录中查找可能的日志文件"""
    state_path = Path(state_dir) if state_dir else Path()
    found = []

    for pattern in LOG_FILE_PATTERNS:
        matches = list(state_path.glob(pattern))
        for m in matches:
            if m.is_file() and m.stat().st_size > 0:
                found.append(m)

    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _analyze_with_engine(log_path: Path) -> dict[str, Any]:
    """使用通用诊断引擎分析日志"""
    try:
        engine = DiagnoseEngine.from_yaml_patterns()
    except Exception as e:
        # A broken optional rule file must not disable the built-in detector,
        # but silently dropping configured rules would hide deployment drift.
        log.warning("诊断 YAML 规则加载失败，回退到内置规则: %s", e)
        engine = DiagnoseEngine()
    try:
        report = engine.analyze_log(str(log_path))
        return report  # already a dict
    except Exception as e:
        log.warning(f"诊断引擎分析失败: {e}")
        return {"error": str(e), "findings": []}


def _bounded_diagnostic_text(value: Any, limit: int) -> str:
    """Collapse untrusted rule/log text and cap one injected field."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def generic_failure_detector_on_turn_start(ctx: HookContext) -> list | None:
    """
    每轮开始时检查运行日志是否有错误迹象。
    使用通用诊断引擎，支持任意工具。
    """
    state_dir = getattr(ctx.state, "root", None) or getattr(ctx.state, "state_dir", None)
    if not state_dir:
        return None

    log_files = _find_log_files(str(state_dir))
    if not log_files:
        return None

    # 分析最新的日志文件
    latest_log = log_files[0]
    diagnosis = _analyze_with_engine(latest_log)

    findings = diagnosis.get("findings", [])
    if not findings:
        return None

    # 筛选严重问题
    critical = [f for f in findings if f.get("severity") == "critical"]
    errors = [f for f in findings if f.get("severity") == "error"]

    if not critical and not errors:
        return None

    # 构建注入消息
    issues = critical[:2] + errors[:2]  # 最多 4 个问题
    issue_lines = []
    for f in issues:
        line = (
            f"  - [{f.get('category', 'unknown')}] "
            f"{_bounded_diagnostic_text(f.get('message'), 80)}"
        )
        generic_fix = _bounded_diagnostic_text(f.get("generic_fix"), 160)
        context_hint = _bounded_diagnostic_text(f.get("context_hint"), 160)
        if generic_fix:
            line += f"\n    修法: {generic_fix}"
        if context_hint:
            line += f"\n    上下文: {context_hint}"
        issue_lines.append(line)

    injection = {
        "role": "system",
        "content": (
            f"[failure_detector] 检测到 {len(critical)} 个严重问题，{len(errors)} 个错误\n"
            f"日志文件: {latest_log.name}\n"
            f"问题摘要:\n" + "\n".join(issue_lines) + "\n"
            f"建议: 检查日志确认，如有严重错误考虑 abort 或记录失败。"
        ),
    }
    return [injection]


def generic_failure_detector_on_end(ctx: HookContext, loop_result: Any) -> None:
    """loop 结束时记录错误检查摘要"""
    state_dir = getattr(ctx.state, "root", None) or getattr(ctx.state, "state_dir", None)
    if not state_dir:
        return

    log_files = _find_log_files(str(state_dir))
    if not log_files:
        return

    diagnosis = _analyze_with_engine(log_files[0])
    stats = diagnosis.get("statistics", {})

    if not hasattr(ctx.state, "_hook_notes"):
        ctx.state._hook_notes = {}

    ctx.state._hook_notes["failure_detector"] = {
        "log_file": str(log_files[0]),
        "critical_count": stats.get("critical_count", 0),
        "error_count": stats.get("error_count", 0),
        "warning_count": stats.get("warning_count", 0),
    }

    if stats.get("critical_count", 0) > 0:
        log.warning("failure_detector: 检测到 %d 个严重问题", stats["critical_count"])


def _generic_failure_detector_candidate_on_turn_start(ctx: HookContext) -> None:
    _supervisor_offer_result(ctx, source="generic_failure", priority=21,
                             result=generic_failure_detector_on_turn_start(ctx))


# 注册通用错误检测器
generic_failure_detector = LoopHook(
    name="generic_failure_detector",
    description="通用运行时错误检测 —— 使用诊断引擎分析日志",
    on_turn_start=_generic_failure_detector_candidate_on_turn_start,
    on_end=generic_failure_detector_on_end,
)
register_loop_hook(generic_failure_detector)


# 保持向后兼容的别名
wrf_failure_detector = generic_failure_detector


# ─────────────────────────────────────────────
# repeated_error_detector —— 同一报错重复出现时强制换方向
# ─────────────────────────────────────────────

import re as _re

_SEEN_ERRORS_KEY = "_repeated_error_seen"   # dict[signature → {"first_turn": int, "count": int}]
_ALERTED_KEY     = "_repeated_error_alerted"  # dict[signature → 已警告过的最高档位 count]

# 分级阈值：同一错误签名累计出现 N 次 → 注入对应等级的强制指令
_ESCALATION_TIERS = (2, 3, 5)

_BUILD_KEYWORDS = {"make", "cmake", "nvfortran", "mpif90", "nvc", "gcc",
                   "gfortran", "mpicc", "mpicxx", "mpif77", "nvcpp", "nvcc"}

# 全命令字符串中搜索的关键词（覆盖 export PATH=... && make 这类多行脚本）
_BUILD_SUBSTRINGS = ("make ", "cmake ", "nvfortran", "mpif90 ", "mpif77 ",
                     "mpicc ", "mpicxx ", "gfortran ", "nvcc ", "nvc ")

# --version / --help 等查询调用不算构建
_VERSION_FLAGS = ("--version", "-version", "--help", "-help", "--show", "-show",
                  "-v ", " -V ", "version 2>&1", "--query")


def _is_build_cmd(cmd: str) -> bool:
    cmd_lower = cmd.lower()
    # 如果整条命令只是查版本/帮助，不视为构建
    cmd_stripped = cmd.strip()
    if any(f in cmd_stripped for f in _VERSION_FLAGS):
        # 只有 which/--version/--help 且不含 make/cmake 真实构建词时跳过
        has_real_build = any(k in cmd_lower for k in ("make ", "cmake ", " -c ", ".f90", ".f ", ".c "))
        if not has_real_build:
            return False
    # 先看第一个有效 token（跳过注释行和空行）
    for line in cmd.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            first_token = stripped.split()[0].split("/")[-1].lower()
            if first_token in _BUILD_KEYWORDS:
                return True
            break
    # 再全字符串扫描（覆盖 export ... && make ... 多行脚本）
    return any(k in cmd_lower for k in _BUILD_SUBSTRINGS)


# 非构建命令但需要检测重复失败的错误模式（系统依赖缺失、环境问题）
_GENERAL_ERROR_PATTERNS = [
    # Perl 模块缺失
    (_re.compile(r"Can't locate ([\w/]+\.pm)", _re.I), "perl_module_missing:{}"),
    # Python 模块缺失
    (_re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'"), "python_module_missing:{}"),
    # 命令未找到
    (_re.compile(r"([\w\-]+): command not found"), "cmd_not_found:{}"),
    # conda/pip 安装失败
    (_re.compile(r"ERROR: Could not find a version that satisfies"), "pip_no_version"),
    (_re.compile(r"PackagesNotFoundError"), "conda_pkg_not_found"),
]


def _extract_error_signature(result: dict) -> str | None:
    """从命令结果中提取关键报错签名，覆盖编译错误 + 系统依赖缺失。"""
    if not isinstance(result, dict) or result.get("returncode", 0) == 0:
        return None
    # timeout 本身也算失败信号，但签名用特殊值
    if result.get("status") == "timeout":
        cmd = result.get("cmd", "")[:60].strip()
        return f"timeout:{cmd[:50]}"

    stderr = result.get("stderr_tail", "") or result.get("stdout_tail", "") or ""
    output = stderr

    # 1. 通用模式（不要求是构建命令）
    for pattern, tmpl in _GENERAL_ERROR_PATTERNS:
        m = pattern.search(output)
        if m:
            sig = tmpl.format(m.group(1)) if "{}" in tmpl else tmpl
            return sig[:120]

    # 2. 构建命令专属错误（error: / fatal:）
    cmd = result.get("cmd", "")
    if not _is_build_cmd(cmd):
        return None
    for line in stderr.splitlines():
        low = line.lower()
        if "error:" in low or "fatal error" in low or "fatal:" in low:
            clean = _re.sub(r'^[^:]*:\d+:\d*:?\s*', '', line).strip()
            clean = _re.sub(r'^(error|fatal error|fatal):\s*', '', clean, flags=_re.I).strip()
            if len(clean) > 8:
                return clean[:120]
    return None


_LAST_DIAG_KEY = "_repeated_error_last_diag"   # 上一轮失败的诊断摘要（供 on_turn_start 注入）


def _is_failure(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("status") in ("error", "timeout"):
        return True
    rc = result.get("returncode")
    return rc is not None and rc != 0


def _result_error_text(result: dict) -> str:
    """失败正文。stderr/stdout tail 优先，都没有时回退 `error` 字段。

    guard 拦截 / 高危 gate / 工具层错误**没有 stderr_tail** —— 正文在 `error` 里。
    2026-07-31 之前 _diagnose_failure 与 _short_error 都只读 tail，后果（实测
    run 1785469523-90f86c）：
      - repeated_error_detector 拿到空串 → 指纹 confidence=low → 永不硬计数，
        同一路径被拦 4 次一次都没归并；
      - execution_control 的 evidence 恒为 "(no stderr/stdout tail)"。

    回退 `error` 时只取首个空行之前的摘要段：工具错误约定是"摘要 + 空行 + 指引"，
    把整段（含标准改写指引）灌进上下文纯属噪音。
    """
    if not isinstance(result, dict):
        return ""
    for key in ("stderr_tail", "stdout_tail"):
        text = (result.get(key) or "").strip()
        if text:
            return text
    error = (result.get("error") or "").strip()
    if not error:
        return ""
    return error.split("\n\n", 1)[0].strip() or error


def _scope_prefix(state) -> str:
    """计数 scope：build_target > route > stage > run（闭环2 build_state 就绪前退化到 run）。"""
    bs = (state.hook_state.get("build_state") or {})
    return (bs.get("build_target") or bs.get("route") or bs.get("stage") or "run")


def _diagnose_failure(record: dict, state) -> dict | None:
    """对一条失败的 run_bash 做诊断：focus_error + fault_fingerprint + 硬判重资格。

    返回 {counter_key, is_hard, focus, fp, log_path, log_sha256} 或 None（非失败）。
    """
    result = record.get("result", {})
    if not _is_failure(result):
        return None
    cmd = result.get("cmd") or (record.get("args", {}) or {}).get("cmd", "")

    # 框架 guard 拦截：命令根本没执行，没有 symbol/source 可供编译型指纹定位。
    # 但 (kind, scope, target) 是完全确定的结构化事实 —— 同一三元组重复出现就是
    # 同一个 blocker，直接给高置信度 key，不走文本指纹。
    blocker = result.get("blocker")
    if isinstance(blocker, dict) and blocker.get("kind"):
        key = (f"{blocker['kind']}|{blocker.get('scope') or '?'}"
               f"|{blocker.get('target') or '?'}")
        return {
            "counter_key": f"{_scope_prefix(state)}|{key}",
            "is_hard": True,
            "focus": {"primary_error": _result_error_text(result)[:220]},
            "fp": {"strict_key": key, "family_key": key,
                   "confidence": "high", "error_type": blocker["kind"]},
            "log_path": None, "log_sha256": None,
        }

    log_path = result.get("log_path")
    focus = None
    if log_path:
        try:
            focus = focus_error(log_path=log_path)
        except Exception:
            focus = None
    err_text = ""
    if focus and focus.get("primary_error"):
        err_text = focus["primary_error"]
    else:
        err_text = _result_error_text(result)
    bs = state.hook_state.get("build_state") or {}
    fp = fault_fingerprint(cmd, err_text, stage=bs.get("stage"))
    hard = is_hard_repeat_eligible(fp)
    counter_key = (f"{_scope_prefix(state)}|{fp['strict_key']}" if hard
                   else None)   # 低置信度不参与硬计数
    # 049-3：工具结果里已经算好的配置修法（safe_bash._attach_diagnosis）原样带上，
    # 升级/软提示时一起注入——不在这里重算，两处口径必然一致。
    guidance = result.get("diagnosis") if isinstance(result.get("diagnosis"), list) else []
    return {"counter_key": counter_key, "is_hard": hard, "focus": focus,
            "fp": fp, "log_path": log_path, "log_sha256": result.get("log_sha256"),
            "guidance": guidance[:2]}


def _focus_block(diag: dict) -> str:
    """注入给模型的聚焦摘要（只含 primary error + focus 段 + 日志 path/hash，不含完整日志）。"""
    parts = []
    focus = diag.get("focus")
    if focus and focus.get("primary_error"):
        parts.append(f"聚焦错误: {focus['primary_error']}")
    if focus and focus.get("focus"):
        parts.append("```\n" + focus["focus"][:600] + "\n```")
    for item in diag.get("guidance") or []:
        if not isinstance(item, dict):
            continue
        fix = _bounded_diagnostic_text(item.get("fix"), 160)
        context = _bounded_diagnostic_text(item.get("context"), 160)
        if fix:
            parts.append(f"修法: {fix}")
        if context:
            parts.append(f"上下文: {context}")
    if diag.get("log_path"):
        sha = diag.get("log_sha256")
        parts.append(f"完整日志(未注入正文): {diag['log_path']}"
                     + (f" sha256={sha[:12]}…" if sha else ""))
    return "\n".join(parts) if parts else "(无聚焦信息)"


def repeated_error_detector_on_turn_end(ctx: HookContext) -> list | None:
    """对失败的 run_bash 做诊断：高置信度指纹按 scoped strict_key 硬计数；低置信度只存诊断态。"""
    if _bench_off("repeated_error"):
        return None
    hook_state = ctx.state.hook_state
    seen: dict = hook_state.setdefault(_SEEN_ERRORS_KEY, {})

    for record in ctx.tool_call_records:
        if record.get("name") not in _BASH_TOOL_NAMES:
            continue
        diag = _diagnose_failure(record, ctx.state)
        if diag is None:
            continue
        # 保存最新诊断态（供 on_turn_start 注入聚焦错误）
        hook_state[_LAST_DIAG_KEY] = diag
        if not diag["is_hard"]:
            continue   # 低置信度：不进硬计数（只软提示）
        key = diag["counter_key"]
        entry = seen.get(key)
        if not isinstance(entry, dict):
            entry = {"first_turn": ctx.turn, "count": 0}
        entry["count"] += 1
        seen[key] = entry
    return None


def _escalation_message(sig: str, entry: dict, tier: int) -> str:
    """按错误重复次数分级生成强制指令。tier ∈ _ESCALATION_TIERS。"""
    head = (
        f"[repeated_error_detector] ⚠️ 同一错误第 **{entry['count']} 次**出现"
        f"（首次于 Turn {entry['first_turn']}）：\n  `{sig[:80]}`\n\n"
    )
    # 知识正本在 SKILL §Diagnostic Discipline D2——这里只给证据 + 一行要点 + 指针
    if tier == 2:
        return head + (
            "修复未触及根因，**强制换方向**（禁止同思路重试）。"
            "按 SKILL §Diagnostic Discipline D2 第 2 档执行。"
        )
    if tier == 3:
        return head + (
            "**3 次=当前思路确认无效。** 先 web_search 报错原文 + scratchpad 写新假设，"
            "否则禁止任何修复尝试。详见 SKILL §D2 第 3 档。"
        )
    return head + (
        f"**预算耗尽（{entry['count']} 次）。** 只允许：记录 dead_end 后换路线 / "
        "在 prereg 允许时调整范围 / 对确有不可替代的外部选择走受控升级路径，**禁止再试**。"
        "详见 SKILL §D2 第 5 档。"
    )


def repeated_error_detector_on_turn_start(ctx: HookContext) -> list | None:
    """与 on_turn_end 用同一 _diagnose_failure / counter_key（修 key 不一致）。

    两遍扫描：先高置信度硬升级，再低置信度软提示（修"soft 在前遮挡 hard"）。
    _diagnose_failure 是纯诊断函数（不写 seen/count），此处只读 seen，不重复计数。
    """
    if _bench_off("repeated_error"):
        return None
    hook_state = ctx.state.hook_state
    seen: dict = hook_state.get(_SEEN_ERRORS_KEY, {})
    alerted: dict = hook_state.setdefault(_ALERTED_KEY, {})
    if isinstance(alerted, set):   # 兼容旧格式
        alerted = {s: 2 for s in alerted}
        hook_state[_ALERTED_KEY] = alerted

    diags = []
    for record in ctx.tool_call_records:
        if record.get("name") not in _BASH_TOOL_NAMES:
            continue
        d = _diagnose_failure(record, ctx.state)
        if d is not None:
            diags.append(d)

    # 第一遍：高置信度硬升级（不被低置信度提前 return 遮挡）
    for d in diags:
        if not d["is_hard"]:
            continue
        key = d["counter_key"]
        entry = seen.get(key)
        if not isinstance(entry, dict) or entry.get("count", 0) < 2:
            continue
        tier = max((t for t in _ESCALATION_TIERS if entry["count"] >= t), default=None)
        if tier is None or alerted.get(key, 0) >= tier:
            continue
        alerted[key] = tier
        return [_sys(_escalation_message(key, entry, tier) + "\n" + _focus_block(d)
                     + _dead_end_block(ctx.state, ctx.messages))]   # 事件触发查 dead_end

    # 第二遍：无硬升级 → 低置信度软提示（每 family 一次，不计硬升级）
    for d in diags:
        if d["is_hard"]:
            continue
        focus = d.get("focus")
        if not (focus and focus.get("primary_error")):
            continue   # 无可聚焦诊断（如 LLM 输出损坏/无日志）→ 不注入"无聚焦信息"纯噪声（agent 已知失败）
        soft_key = "soft|" + (d["fp"].get("family_key") or d["fp"].get("error_type") or "?")
        if alerted.get(soft_key) != "soft":
            alerted[soft_key] = "soft"
            return [_sys(
                "[repeated_error] 上一条命令失败（指纹低置信度，无法稳定判重，不计入硬升级）：\n"
                + _focus_block(d) + "\n建议：web_search 报错原文，或补充上下文后重试。")]
    return None


def _repeated_error_candidate_on_turn_start(ctx: HookContext) -> None:
    _supervisor_offer_result(ctx, source="repeated_error", priority=30,
                             result=repeated_error_detector_on_turn_start(ctx))


repeated_error_detector = LoopHook(
    name="repeated_error_detector",
    description="同一报错重复出现时分级强制升级：2次换方向 / 3次禁止重试先search / 5次只允许换路线或受控升级",
    on_turn_start=_repeated_error_candidate_on_turn_start,
    on_turn_end=repeated_error_detector_on_turn_end,
)
register_loop_hook(repeated_error_detector)


# ─────────────────────────────────────────────
# strategic_review_injector —— 长期未收敛时强制战略复盘
# ─────────────────────────────────────────────
# 设计动机：错误可以每轮变化（表面上有进展），但如果任意命令持续失败
# 而没有推进里程碑，说明在横向漂移而非向目标收敛。
# 每累计 _REVIEW_INTERVAL 轮有任意 run_bash 失败，注入一次强制战略复盘。

_REVIEW_FAIL_TURNS_KEY = "_strategic_fail_turns"   # 累计有失败的轮次数
_REVIEW_LAST_TURN_KEY  = "_strategic_last_inject"  # 上次注入时的轮次
_REVIEW_INTERVAL = 10  # 每累计 10 轮有失败触发一次

# 节奏触发：每 N 轮无条件复盘一次路线。
# 设计动机：防止长时间构建在同一战略错误上反复消耗轮次
# 局部上都是"成功"的（returncode=0），失败计数永远不累积——错的是计划
# 而不是步骤。失败触发覆盖不了这个盲区，必须有与成败无关的节奏触发。
_CADENCE_LAST_KEY = "_strategic_last_cadence"
_CADENCE_INTERVAL = 40


# ── 第4步：declared vs observed route 冲突 + 连续无里程碑（最小，soft）──────────
_OBSERVED_ROUTE_KEY = "_strategic_observed_route"   # 最近观察到的路线
_NO_PROGRESS_KEY = "_strategic_no_progress"          # 连续主要 build 无新产物的轮次
_ROUTE_CONFLICT_ALERTED = "_strategic_route_conflict_alerted"
_NO_PROGRESS_THRESHOLD = 5                            # 连续 5 轮 build 无里程碑 → soft 复盘


_INSPECT_CMDS = frozenset((
    "ls", "cat", "grep", "find", "head", "tail", "echo", "which", "file", "stat",
    "wc", "nm", "pwd", "awk", "sed", "less", "more", "tree", "du", "env", "cd", "test", "[",
))


def _observed_route(cmd: str) -> str | None:
    """从命令粗粒度推断实际走的路线（与 source_recon 的 5 类对齐，最小版）。"""
    c = (cmd or "").lower()
    # 纯探查命令（所有 &&/;/| 段首词都是只读命令）→ 不算任何构建路线。
    # 通用消除"构建工具名出现在文件名参数里"的子串污染（如 grep meson.build / cat *.cmake）。
    segs = [s.strip() for s in _re.split(r"&&|\|\||;|\|", c)]
    if segs and all((not s) or s.split()[0] in _INSPECT_CMDS for s in segs):
        return None
    if "case.build" in c or "create_newcase" in c or "buildlib" in c:
        return "official_build_system"
    if _re.search(r"(nvfortran|gfortran|mpif90|mpifort|gcc|g\+\+|nvc)\b[^\n]*\s-c(?:\s|$)", c) \
            and not _re.search(r"\bcmake\b", c) and not _re.search(r"\bmake\b", c):
        return "manual_component_build"   # 手工逐文件编译
    # 项目原生构建系统（cmake/make/ninja/meson/autotools/mkmf 同归 project_native_manual route）。
    # 词边界匹配：避免 "cmake" 子串误命中 CMakeLists.txt（dry-run 实证的探查命令污染）。
    # meson/configure/mkmf 补齐：原本它们返回 None（observed 信号缺失），现给出完整路线信号。
    if (_re.search(r"\bcmake\b", c) or _re.search(r"\bmake\b", c) or _re.search(r"\bninja\b", c)
            or _re.search(r"\bmeson\b", c) or _re.search(r"(^|[\s;&|])\./configure\b", c)
            or _re.search(r"\bautoreconf\b", c) or _re.search(r"\b(mkmf|list_paths)\b", c)):
        return "project_native_manual"
    if "spack install" in c or "conda install" in c:
        return "package_manager"
    return None


def _declared_route_type(state) -> str | None:
    """读 declared_route artifact，提取声明的路线类型（5 类关键词）。"""
    # canonical v2 已用 steps/action/effects 表达执行子图，没有旧
    # route_type 轴。不得再从其 goal/evidence 文本里猜一个旧类型，
    # 否则会产生与权威 resolver 竞争的路线冲突提示。
    try:
        try:
            from .tools.execution_route import load_canonical_route
        except ImportError:
            from tools.execution_route import load_canonical_route
        loaded = load_canonical_route(state)
        if (
            loaded.get("status") == "ready"
            and loaded.get("source_format") == "v2"
        ):
            return None
    except Exception:
        pass
    try:
        for a in state.list_artifacts():
            if a.get("type") == "declared_route":
                rec = state.read_artifact(a.get("id") or a.get("name"))
                content = str((rec or {}).get("content") or "")
                try:
                    from tools.build_contract import parse_contract
                    contract = parse_contract(content)
                    val = str(contract.get("route_type") or "").lower()
                    if val in ("official_build_system", "manual_component_build",
                               "project_native_manual", "package_manager", "container"):
                        return val
                except Exception:
                    pass
                # 精确提取 route 字段值，不全文找关键词（否则误匹配描述里的其它 route 名）。
                # 兼容 JSON("route":)、YAML(route:)、Markdown(- **route**:) 三种 agent 实际写法。
                m = _re.search(r"route[*\"'\s]*[:=][*\"'\s]*([a-z_]+)", content, _re.I)
                if m:
                    val = m.group(1).lower()
                    for rt in ("official_build_system", "manual_component_build",
                               "project_native_manual", "package_manager", "container"):
                        if val == rt:
                            return rt
    except Exception:
        pass
    return None


# ── 第5步：最小里程碑（A 确定性判 configured/dependency-generated/target-built，B 兜底 min-run）──
_CONFIG_MARKERS = ("CMakeCache.txt", "Makefile", "config.status", "config.h", "build.ninja")
_MILESTONE_ORDER = ("configured", "dependency-generated", "target-built", "min-run")


def _detect_milestones(source_path: str, since_ts: float, target_path: str | None = None) -> set:
    """确定性判定本轮新达成的里程碑（A 为主）。只算 since_ts 之后、build 目录内的产物。"""
    import os
    achieved: set = set()
    if not source_path or not os.path.isdir(source_path):
        return achieved
    scan_dirs = [source_path]
    bd = os.path.join(source_path, "build")
    if os.path.isdir(bd):
        scan_dirs.append(bd)
    for base in scan_dirs:
        for root, dirs, files in os.walk(base):
            if root[len(base):].count(os.sep) >= 4:
                dirs[:] = []
            dirs[:] = [x for x in dirs if x not in (".git", "third_party", "node_modules")]
            for f in files:
                try:
                    mt = os.path.getmtime(os.path.join(root, f))
                except OSError:
                    continue
                if mt <= since_ts:        # 旧文件/临时探针不算（只本轮新生成）
                    continue
                if f in _CONFIG_MARKERS:
                    achieved.add("configured")
                if f.endswith((".o", ".a", ".mod")):
                    achieved.add("dependency-generated")
    # target-built：目标路径由任务/agent 结构化声明，框架核验真实存在且本轮生成（B 兜底）
    if target_path:
        tp = os.path.expanduser(target_path)
        try:
            if os.path.exists(tp) and os.path.getmtime(tp) > since_ts:
                achieved.add("target-built")
        except OSError:
            pass
    return achieved


def verify_min_run(cmd: str, expected_substr: str | None = None, timeout: int = 300,
                   output_paths: list[str] | None = None,
                   cwd: str | None = None) -> dict:
    """兼容哨兵：不再从 hook/execute_python 内部启动隐藏子进程。

    旧实现无法获得 ``State``，因此不能可靠复用 canonical route、路径角色、
    科学契约和 cgroup。保留同名函数只为让旧 fixture 得到可纠偏的明确错误；
    实际最小运行必须直接走 ``safe_run_bash`` 或 ``submit_job``，并绑定
    ``route_step_id``。完成事实由工具收据和路线 ``expected_outputs`` 派生。
    """
    del cmd, expected_substr, timeout, output_paths, cwd
    return {
        "milestone": False,
        "status": "error",
        "reason": "managed_execution_required",
        "error": (
            "verify_min_run 已停止执行命令：请把最小运行声明为 declared_route v2 "
            "步骤，并用 safe_run_bash 或 submit_job 传 route_step_id 执行。"
        ),
    }


def strategic_review_on_turn_end(ctx: HookContext) -> list | None:
    """记录失败轮次 + 观察路线 + 命名里程碑推进（A 确定性）。"""
    if _bench_off("strategic_review"):
        return None
    import time
    hook_state = ctx.state.hook_state
    try:
        try:
            from .tools.execution_route import load_canonical_route
        except ImportError:
            from tools.execution_route import load_canonical_route
        loaded_route = load_canonical_route(ctx.state)
        canonical_v2 = (
            loaded_route.get("status") == "ready"
            and loaded_route.get("source_format") == "v2"
        )
    except Exception:
        canonical_v2 = False
    if canonical_v2:
        # v2 的进度由 route receipt 纯投影。旧 route_type 观察与
        # mtime milestone 只是 legacy 兼容层，不再维护平行 current stage。
        hook_state[_NO_PROGRESS_KEY] = 0
        hook_state[_ROUTE_CONFLICT_ALERTED] = None
    had_failure = False
    had_major_build = False
    for record in ctx.tool_call_records:
        if record.get("name") not in _BASH_TOOL_NAMES:
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        if result.get("returncode", 0) != 0 or result.get("status") in ("error", "timeout"):
            had_failure = True
            try:
                log_text = ""
                lp = result.get("log_path")
                if lp and os.path.isfile(lp):
                    log_text = open(lp, "r", encoding="utf-8", errors="replace").read()[-20000:]
                else:
                    log_text = (result.get("stdout_tail") or "") + "\n" + (result.get("stderr_tail") or "")
                from tools.build_graph import confirmed_edges_from_failure
                edges = confirmed_edges_from_failure(log_text)
                if edges:
                    ctx.state.save_artifact(
                        "build_graph_confirmed_edge",
                        f"confirmed_dependency_edge_{ctx.turn}",
                        json.dumps({"schema_version": "1.0", "edges": edges}, ensure_ascii=False, indent=2),
                        metadata={"turn": ctx.turn, "source": "failure_log",
                                  "log_path": lp, "confidence": "confirmed"})
                    log.info("confirmed dependency edge(s) from failure log: %s", edges)
            except Exception:
                pass
        cmd = result.get("cmd") or (record.get("args", {}) or {}).get("cmd", "")
        obs = _observed_route(cmd) if not canonical_v2 else None
        if obs:
            hook_state[_OBSERVED_ROUTE_KEY] = obs
            if obs in ("official_build_system", "manual_component_build", "project_native_manual"):
                had_major_build = True   # 用 _observed_route 判（_is_build_cmd 对结尾 make 漏判）

    if had_failure:
        hook_state[_REVIEW_FAIL_TURNS_KEY] = hook_state.get(_REVIEW_FAIL_TURNS_KEY, 0) + 1

    # 命名里程碑：本轮有主要 build → 确定性判 configured/dependency-generated/target-built。
    # 有新里程碑 → 写入 build_state 并重置无进度计数；无新里程碑 → 累加（soft 提示用）。
    if had_major_build:
        sp = None
        target_path = None
        try:   # 从记录读 metadata.source_path（list_artifacts 摘要不含 metadata）
            for kind in ("source_recon", "platform_profile"):
                for entry in ctx.state.list_artifacts(kind, own_only=True):
                    meta = (ctx.state.read_artifact(entry["id"]) or {}).get("metadata") or {}
                    sp = sp or meta.get("source_path")
                    target_path = target_path or meta.get("target_path")
                    if sp:
                        break
                if sp:
                    break
            # target-built（B 兜底）：agent 在 declared_route artifact 结构化声明的目标路径
            for entry in ctx.state.list_artifacts("declared_route", own_only=True):
                doc = ctx.state.read_artifact(entry["id"]) or {}
                meta = doc.get("metadata", {})
                target_path = target_path or meta.get("target_path")
                if not target_path:
                    try:
                        from tools.build_contract import parse_contract
                        contract = parse_contract(doc.get("content", ""))
                    except Exception:
                        contract = {}
                    for item in contract.get("expected_artifacts") or []:
                        if isinstance(item, dict) and item.get("path"):
                            target_path = str(item["path"])
                            break
                if not target_path:   # 兼容写在正文 `target_path: <path>` 的声明
                    m = _re.search(r"target_path[*\"'\s]*[:=][*\"'\s]*([^\s\"'*]+)", doc.get("content", ""))
                    if m:
                        target_path = m.group(1)
        except Exception:
            pass
        # sp=None（侦察 artifact 尚未生成，如 gate 触发前的 cmake 配置轮）→ 本轮无法扫描。
        # 此时【不推进 mtime 基线、不计无进度】，否则会越过"能扫描之前"生成的产物（configured 漏检根因）。
        if sp:
            last_check = hook_state.get("_strategic_last_product_ts", 0)
            achieved = _detect_milestones(sp, last_check, target_path)
            bs = hook_state.setdefault("build_state", {})
            prev = set(bs.get("milestones", []))
            if achieved - prev:
                bs["milestones"] = sorted(prev | achieved, key=_MILESTONE_ORDER.index)
                bs["stage"] = bs["milestones"][-1]     # 最高里程碑
                log.info("milestone 达成: %s（stage=%s）", sorted(achieved - prev), bs["stage"])
                hook_state[_NO_PROGRESS_KEY] = 0       # 有新里程碑，重置
            else:
                hook_state[_NO_PROGRESS_KEY] = hook_state.get(_NO_PROGRESS_KEY, 0) + 1
            hook_state["_strategic_last_product_ts"] = time.time()
    return None


def strategic_review_on_turn_start(ctx: HookContext) -> list | None:
    """两种触发：① 累计失败轮次达阈值；② 每 _CADENCE_INTERVAL 轮无条件路线复盘。"""
    if _bench_off("strategic_review"):
        return None
    hook_state = ctx.state.hook_state
    # resume 时 hook_state 保留了上一轮的计数，Turn 1 清零避免立刻爆发
    if ctx.turn == 1:
        hook_state[_REVIEW_FAIL_TURNS_KEY] = 0
        hook_state[_REVIEW_LAST_TURN_KEY] = -999
        # 节奏复盘从本 run 第 1 轮起算：原先置 -999，第 1 轮就满足「距上次 ≥ 40 轮」，
        # 例行复盘在还没做任何事时就注入（收敛任务书 K1）。
        hook_state[_CADENCE_LAST_KEY] = 0
    if ctx.turn == 1:
        hook_state[_NO_PROGRESS_KEY] = 0
        hook_state[_ROUTE_CONFLICT_ALERTED] = None
        # 里程碑基线：只统计本次运行开始之后新生成的产物（排除上次运行遗留的旧文件）
        import time as _t
        hook_state["_strategic_last_product_ts"] = _t.time()
    fail_turns = hook_state.get(_REVIEW_FAIL_TURNS_KEY, 0)
    last_inject = hook_state.get(_REVIEW_LAST_TURN_KEY, -999)

    # 触发 ⓪a：declared vs observed route 冲突（第4步，soft，每对冲突注一次）
    declared = _declared_route_type(ctx.state)
    observed = hook_state.get(_OBSERVED_ROUTE_KEY)
    if declared and observed and declared != observed:
        pair = f"{declared}->{observed}"
        if hook_state.get(_ROUTE_CONFLICT_ALERTED) != pair:
            hook_state[_ROUTE_CONFLICT_ALERTED] = pair
            return [_sys(
                f"[strategic_review] ⚠️ 路线冲突：你声明的 declared_route 是 **{declared}**，"
                f"但最近命令实际在走 **{observed}**。"
                "若确实要切换路线，请更新 declared_route artifact 并说明理由（route_evidence / "
                "acknowledged_risks）；否则回到声明的官方路线。"
                "（典型反模式：声明用官方构建系统却转手工逐文件编译，参见 KB dead_end。）"
                + _dead_end_block(ctx.state, ctx.messages)   # route_change 事件查 dead_end
            )]

    # 触发 ⓪b：连续无里程碑（第4步，soft）——主要 build 多轮但无新产物
    if hook_state.get(_NO_PROGRESS_KEY, 0) >= _NO_PROGRESS_THRESHOLD:
        hook_state[_NO_PROGRESS_KEY] = 0
        return [_sys(
            f"[strategic_review] ⚠️ 已连续 {_NO_PROGRESS_THRESHOLD} 轮主要 build 无新构建产物"
            "（.o/.a/.mod/可执行）。局部命令可能成功但整体未推进。"
            "**暂停复盘**：当前路线还能产出里程碑吗？是否该 switch/escalate？写进 scratchpad。"
            + _dead_end_block(ctx.state, ctx.messages)   # no_progress 事件查 dead_end
        )]

    # 触发 ①：失败累积（避免和上次注入太近，至少间隔 5 轮）
    if fail_turns >= _REVIEW_INTERVAL and (ctx.turn - last_inject) >= 5:
        hook_state[_REVIEW_FAIL_TURNS_KEY] = 0
        hook_state[_REVIEW_LAST_TURN_KEY] = ctx.turn
        hook_state[_CADENCE_LAST_KEY] = ctx.turn   # 失败复盘也算一次节奏复盘
        # 知识正本在 SKILL §Diagnostic Discipline D3——这里只给触发证据 + 指针
        return [_sys(
            f"[strategic_review] ⚠️ 已累计 {_REVIEW_INTERVAL} 轮有命令失败。"
            "**暂停，按 SKILL §Diagnostic Discipline D3 四问复盘**，"
            "决定 continue/switch/escalate 并写进 scratchpad。"
            "无新信息（web_search / request_human_input）不得重试已失败思路。"
        )]

    # 触发 ②：节奏复盘 —— 与成败无关，检查路线本身是否偏离 prereg
    last_cadence = hook_state.get(_CADENCE_LAST_KEY, 0)
    if ctx.turn - last_cadence >= _CADENCE_INTERVAL:
        hook_state[_CADENCE_LAST_KEY] = ctx.turn
        return [_sys(
            f"[strategic_review] 📋 例行路线复盘（每 {_CADENCE_INTERVAL} 轮，与成败无关——"
            f"局部步步成功不代表路线正确）。已花费 {ctx.turn} 轮。"
            "**按 SKILL §Diagnostic Discipline D3 四问回答并写进 scratchpad**；"
            "上次复盘以来无里程碑（编译产物/可运行二进制/成功输出）→ 默认 switch 或 escalate。"
        )]
    return None


def _strategic_review_candidate_on_turn_start(ctx: HookContext) -> None:
    _supervisor_offer_result(ctx, source="strategic_review", priority=40,
                             result=strategic_review_on_turn_start(ctx))


strategic_review_injector = LoopHook(
    name="strategic_review_injector",
    description="长期编译失败未收敛时强制战略复盘，防止横向漂移",
    on_turn_start=_strategic_review_candidate_on_turn_start,
    on_turn_end=strategic_review_on_turn_end,
)
register_loop_hook(strategic_review_injector)


def _provisioned_build_tracker_on_turn_end(ctx: HookContext) -> list | None:
    """provision-first：纯证据推进 build_state + env.sh 变更触发 stale。

    必须独立于 strategic_review：这是 build 状态推进/失效传播，不是复盘提示。
    用 provision_first 守卫，保证 C/D 组测到同一条 provision 主路径。
    """
    if _bench_off("provision_first"):
        return None
    try:
        from tools.build_state import (load_state_from_hook, save_state_to_hook,
                                        framework_verify_outputs, advance_node,
                                        mark_stale_on_env_change, mark_inherited,
                                        resolve_run_started_at, adopt_inherited_nodes)
        from tools.env_provision import env_path_for_state, env_info, env_fingerprint
    except Exception:
        return None
    hs = ctx.state.hook_state
    st = load_state_from_hook(hs)
    if not st:
        return None   # 还没抽到 DAG，无状态可推进

    # Wire 3: env.sh 内容变了 → 重探 + 标 stale（仅在真变时重探，开销有界）
    try:
        env_path = env_path_for_state(ctx.state)
        cur_sha = env_info(env_path)["sha256"]
        if not st.get("env_sha256"):
            st["env_sha256"] = cur_sha            # 首轮基线，不标 stale
        elif cur_sha != st["env_sha256"]:
            try:
                from tools.repro_snapshot import collect_platform_profile
                prof = collect_platform_profile(env_path=str(env_path))
            except Exception:
                prof = {}
            new_fp = env_fingerprint(env_path, prof)
            mark_stale_on_env_change(st, new_fp)   # 已 built/verified + 下游 → stale
            st["env_sha256"] = cur_sha
            log.info("env/build_env.sh changed → build_state marked stale (fp=%s)", new_fp)
    except Exception:
        pass

    # Wire 1: node.outputs 真存在 → verified（framework 证据推进，不靠 agent 文字）。
    # E-11：产物存在但 mtime 早于本 run 起始 → inherited（跨 run 遗留），不反向背书
    # verified；产物 metadata 声明 reused_inputs 精确覆盖后由 adopt_inherited_nodes
    # 机械收养。锚不可得（=0）时新鲜度检查退化为旧的存在性检查，留痕一次。
    try:
        anchor = resolve_run_started_at(ctx.state)
        if anchor <= 0 and not hs.get("_provision_run_anchor_missing_logged"):
            hs["_provision_run_anchor_missing_logged"] = True
            ctx.state.append_transcript(
                "provision_run_anchor_unavailable",
                note="run 起始锚不可得，产物新鲜度检查退化为存在性检查（旧行为）")
        for nid, node in (st.get("nodes") or {}).items():
            if node.get("state") in ("planned", "configured", "built", "inherited") \
                    and node.get("outputs"):
                res = framework_verify_outputs(node["outputs"], run_started_at=anchor)
                if not res.get("ok"):
                    continue
                stale = [r for r in res["outputs"] if r.get("predates_run")]
                if stale:
                    if node.get("state") != "inherited":
                        mark_inherited(st, nid, res, anchor, turn=ctx.turn)
                        ctx.state.append_transcript(
                            "provisioned_node_inherited", node=nid,
                            run_started_at=anchor,
                            outputs=[{"path": r["path"], "mtime": r.get("mtime")}
                                     for r in res["outputs"]])
                    else:
                        # 部分重建后 stale 集缩小：刷新事实，收养门槛随之缩小
                        node["inherited_outputs"] = stale
                else:
                    advance_node(st, nid, "verified",
                                 {"by": "framework_verify_outputs", "turn": ctx.turn})
        # 耦合放宽（同 commit 上线）：产物 metadata 已声明 reused_inputs → 机械收养
        if any(n.get("state") == "inherited" for n in (st.get("nodes") or {}).values()):
            try:
                from core.data_provenance import (load_full_artifacts,
                                                  declared_reuse_paths)
                declared: set[str] = set()
                for a in load_full_artifacts(ctx.state):
                    declared |= declared_reuse_paths(a)
            except Exception:
                declared = set()
            if declared:
                result = adopt_inherited_nodes(st, declared, turn=ctx.turn)
                for rec in result.get("adopted") or []:
                    ctx.state.append_transcript(
                        "reused_inputs_adoption", node=rec["node"],
                        adopted_paths=rec["adopted_paths"])
                for rec in result.get("incomplete") or []:
                    # 部分覆盖不放行，但绝不静默失败：留下 uncovered/declared 事实
                    key = repr((rec["node"], rec["uncovered_paths"],
                                rec["declared_paths"]))
                    logged = hs.setdefault(
                        "_reused_inputs_adoption_incomplete_logged", [])
                    if key not in logged:
                        logged.append(key)
                        ctx.state.append_transcript(
                            "reused_inputs_adoption_incomplete", node=rec["node"],
                            uncovered_paths=rec["uncovered_paths"],
                            declared_paths=rec["declared_paths"])
    except Exception:
        pass

    save_state_to_hook(hs, st)
    return None


provisioned_build_tracker = LoopHook(
    name="provisioned_build_tracker",
    description="provision-first：DAG 节点证据推进 + env.sh 变更 stale 传播",
    on_turn_end=_provisioned_build_tracker_on_turn_end,
    emits=("provisioned_node_inherited", "provision_run_anchor_unavailable",
           "reused_inputs_adoption", "reused_inputs_adoption_incomplete"),
)
register_loop_hook(provisioned_build_tracker)


# ─────────────────────────────────────────────
# execution_control —— 阶段锁 + issue ledger（通用执行控制层）
# ─────────────────────────────────────────────
# 目标：把长 HPC build 中的"当前焦点/已通过阶段/最新证据"变成确定性状态，
# 减少 LLM 在旧问题之间来回漂移。只维护通用组件名，不内置具体应用知识。

_ENG_KEY = "_execution_control"
_ENG_LAST_INJECT_KEY = "_execution_control_last_inject"
_ENG_SUPPRESS_UNTIL_KEY = "_execution_control_suppress_until"


# 纯探查命令：失败是正常探测结果（`which nvidia-smi` 返回非 0 说明"没装"），
# 不是工程问题，不该进 execution_control 台账。
_PROBE_HEADS = frozenset({
    "which", "type", "whereis", "man", "ls", "cat", "head", "tail", "echo",
    "find", "grep", "test", "stat", "file", "printenv", "env", "id", "uname",
    "df", "free", "nvidia-smi", "lscpu", "clinfo", "ldconfig", "pkg-config",
})
_PROBE_FLAGS = ("--version", "--help", "-dumpversion", "-dumpmachine")


def _is_probe_cmd(cmd: str) -> bool:
    """命令位上是探查工具，或整条只是查版本/帮助。"""
    text = (cmd or "").strip()
    if not text:
        return False
    first = _re.split(r"[/\s]+", text.lstrip("(").lstrip())
    head = ""
    for token in first:
        if _re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        head = token
        break
    head = head.rsplit("/", 1)[-1].lower()
    if head in _PROBE_HEADS:
        return True
    return any(flag in text for flag in _PROBE_FLAGS)


def _component_from_text(log_path: str = "", cmd: str = "") -> str | None:
    """Component name from a build log path.

    只从 **log 路径** 推断（`atm.bldlog` → `atm`）—— 这是本函数原本的设计意图，
    `.bldlog/.bldl/.log` 后缀剥离就是证据。

    2026-07-31 之前它还会把命令按 `[/\\s]+` 切开取 `candidates[-1]`，也就是
    **整条命令的最后一个 token**，于是 run 1785469523-90f86c 里 current_issue
    实测出现 `head`（来自 `| head -2`）、`x86_64-linux-gnu`、以及 `hea`/`Chec`/`cu`
    这类被 `cmd[:160]` 截断切出来的碎片，每轮注入上下文当"当前问题"。
    命令里的最后一个词跟"哪个组件坏了"没有任何关系 —— 推不出就返回 None，
    由调用方退回展示命令本身，别编一个假的组件名。
    """
    if not log_path:
        return None
    name = str(log_path).rsplit("/", 1)[-1].strip()
    name = _re.sub(r"\.(?:bldlog|bldl|log|err|out)(?:\..*)?$", "", name)
    name = _re.sub(r"[^A-Za-z0-9_+\-.]", "", name)
    if name.lower() in {"bash", "build", "bld", "cmake", "make", "case", "run", "tmp"}:
        return None
    if 2 <= len(name) <= 40 and _re.search(r"[A-Za-z]", name):
        return name
    return None


def _short_error(result: dict) -> str:
    """失败输出的一行摘要 —— 取**抬头行**，不猜哪行是错误。

    原来按 `error|fatal|failed|cannot|undefined|missing` 六个英文词挑行；本仓的
    报错是中文的（safe_bash 的 scope guard：`⛔ 可执行目标不存在（exec_preflight
    事前拦截，命令未执行）…`），一行都匹配不上，落到 `splitlines()[-1]` 兜底，
    取到的是最没信息量的那行 `（解析 cwd：…）`；`_component_from_text` 同理认不
    出 → component="unknown" → 账本里永远 open（#625，E2E v28 实拍：run 其实
    成功了，repair_ledger 收尾在 open_components=["unknown"]）。

    "硬编码名单最先漏掉的是自家的东西"。抬头行天然是信息量最高的一行 ——
    不依赖任何词表。
    """
    text = _result_error_text(result).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "(no stderr/stdout tail)"
    # 抬头行打头，后面的行按顺序补进 220 字符预算 —— 错误的**对象**（目标路径、
    # 越界的那个文件）常在第二行，光留抬头会把它丢掉（repeated_error 的指纹靠它）。
    return " ".join(lines)[:220]


def _execution_control_on_turn_end(ctx: HookContext) -> list | None:
    st = ctx.state.hook_state.setdefault(_ENG_KEY, {
        "passed": {},      # component -> {turn, evidence}
        "issues": [],      # newest last
        "current": None,   # latest failing issue
        "issues_seen": 0,  # 含被环形截断掉的，用于诚实标注 truncated
    })
    changed = False
    for rec in ctx.tool_call_records:
        if rec.get("name") not in _BASH_TOOL_NAMES:
            continue
        result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
        args = rec.get("args") or {}
        cmd = result.get("cmd") or args.get("cmd", "")
        comp = _component_from_text(result.get("log_path", ""), cmd)
        failed = result.get("status") in ("error", "timeout") or result.get("returncode") not in (None, 0)

        # attempted_fix 只记**机械可观察**的事实：同一 component 在该 issue 之后
        # 实际跑过哪些命令。不采纳 agent 自述"我试了什么"——那是自我报告。
        if comp:
            for issue in st.get("issues", []):
                if issue.get("component") != comp or issue.get("turn", 0) > ctx.turn:
                    continue
                attempts = issue.setdefault("attempted_fix", [])
                if len(attempts) < 10:
                    attempts.append({
                        "turn": ctx.turn,
                        "cmd": cmd[:160],
                        "outcome": "failed" if failed else "ok",
                    })
                    changed = True

        if failed and _is_probe_cmd(cmd):
            # 探查失败是结论不是故障：`which nvidia-smi` 非 0 只说明没装。
            # 记进台账会把每一次环境探测都变成"当前工程问题"注入上下文。
            continue

        if failed:
            issue = {
                "turn": ctx.turn,
                "component": comp or "unknown",
                "error": _short_error(result),
                "log_path": result.get("log_path"),
                "cmd": cmd[:160],
                "attempted_fix": [],
            }
            st.setdefault("issues", []).append(issue)
            st["issues"] = st["issues"][-20:]
            st["issues_seen"] = int(st.get("issues_seen") or 0) + 1
            st["current"] = issue
            changed = True
        elif comp and _is_build_cmd(cmd):
            st.setdefault("passed", {})[comp] = {
                "turn": ctx.turn,
                "evidence": result.get("log_path") or cmd[:120],
            }
            changed = True
    if changed:
        ctx.state.hook_state[_ENG_KEY] = st
    return None


def _execution_control_on_turn_start(ctx: HookContext) -> list | None:
    suppress_until = ctx.state.hook_state.get(_ENG_SUPPRESS_UNTIL_KEY, -1)
    if ctx.turn <= suppress_until:
        return None
    st = ctx.state.hook_state.get(_ENG_KEY) or {}
    current = st.get("current")
    passed = st.get("passed") or {}
    if not current and not passed:
        return None
    issue_key = None
    if current:
        issue_key = f"{current.get('turn')}|{current.get('component')}|{current.get('error')}"
    else:
        issue_key = "passed|" + ",".join(sorted(passed)[-5:])
    last = ctx.state.hook_state.get(_ENG_LAST_INJECT_KEY)
    # Inject on issue changes, plus a sparse reminder every 15 turns for long builds.
    if last == issue_key and ctx.turn % 15 != 0:
        return None
    ctx.state.hook_state[_ENG_LAST_INJECT_KEY] = issue_key

    lines = ["[execution_control] DO NOT QUOTE THIS BLOCK. Use it only to choose the next tool/action."]
    if passed:
        comps = ", ".join(f"{k}(Turn {v.get('turn')})" for k, v in sorted(passed.items())[-8:])
        lines.append(f"- passed: {comps}; do not re-diagnose unless new evidence invalidates it.")
    if current:
        # component 推不出来时展示命令本身，而不是编一个假组件名（见 _component_from_text）
        label = current.get("component")
        if not label or label == "unknown":
            label = (current.get("cmd") or "?").strip()[:80] or "?"
        lines.append(f"- current_issue: {label} (Turn {current.get('turn')})")
        lines.append(f"- evidence: {current.get('error')}")
        if current.get("log_path"):
            lines.append(f"- evidence_log: {current.get('log_path')}")
        lines.append("- next_action: call a tool, inspect the cited evidence, or write/freeze experiment_log; do not restate this block.")
        lines.append("- fix_order: env/cmd args -> case-local config/build cache -> source-tree config with human approval -> upstream source last.")
    return [_sys("\n".join(lines))]


def _execution_control_on_end(ctx: HookContext, loop_result: Any) -> None:
    """把内存台账落成结构化 artifact —— 失败/延期/dead-end 才可事后复查。

    此前台账只活在 hook_state 里，run 结束即消失，复盘只能靠 scratchpad 散文。
    这里只序列化**机械观察到的**字段；`status` 由"该 component 之后是否出现在
    passed 里"推导，`deferred` / `dead_end` 需要 agent 在 experiment_log 显式声明，
    本 hook 不替它判——推断出来的 dead_end 会污染跨项目复利。
    """
    state = ctx.state
    ledger = state.hook_state.get(_ENG_KEY) or {}
    issues = ledger.get("issues") or []
    passed = ledger.get("passed") or {}
    if not issues and not passed:
        return

    records = []
    for issue in issues:
        component = issue.get("component") or "unknown"
        pass_rec = passed.get(component) or {}
        resolved = bool(pass_rec) and pass_rec.get("turn", -1) >= issue.get("turn", 0)
        records.append({
            "component": component,
            "latest_error": issue.get("error"),
            "attempted_fix": issue.get("attempted_fix") or [],
            "status": "passed" if resolved else "open",
            "status_source": "derived_from_build_evidence",
            "log_path": issue.get("log_path"),
            "turn": issue.get("turn"),
            "resolved_at_turn": pass_rec.get("turn") if resolved else None,
        })

    seen = int(ledger.get("issues_seen") or len(issues))
    truncated = seen > len(issues)
    payload = {
        "schema_version": "repair_ledger.v1",
        "run_id": getattr(state, "run_id", None),
        "issues_recorded": len(records),
        "issues_seen": seen,
        # 截断必须自报：长编译里早期 blocker 会被环形缓冲挤掉，
        # 一份看起来完整的台账比一份标注了截断的台账更危险。
        "truncated": truncated,
        "components_passed": sorted(passed.keys()),
        "open_components": sorted(
            {r["component"] for r in records if r["status"] == "open"}),
        "issues": records,
        "note": ("deferred / dead_end 需由 agent 在 experiment_log 显式声明；"
                 "本台账只推导 passed / open。"),
    }
    try:
        saved = state.save_artifact(
            artifact_type="repair_ledger",
            name="repair_ledger",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            metadata={
                "run_id": payload["run_id"],
                "issues_recorded": len(records),
                "issues_seen": seen,
                "truncated": truncated,
                "open_components": payload["open_components"],
            },
        )
        state.append_transcript(
            "repair_ledger_recorded",
            artifact_id=saved["id"],
            issues_recorded=len(records),
            issues_seen=seen,
            truncated=truncated,
            open_components=payload["open_components"],
        )
    except Exception as exc:
        log.warning("repair_ledger 落盘失败: %s", exc)


def _execution_control_candidate_on_turn_start(ctx: HookContext) -> None:
    _supervisor_offer_result(ctx, source="execution_control", priority=20,
                             result=_execution_control_on_turn_start(ctx))


execution_control = LoopHook(
    name="execution_control",
    description="通用执行控制层：维护已通过组件、当前阻塞 issue 和证据门槛，"
                "run 结束落成结构化 repair_ledger artifact",
    on_turn_start=_execution_control_candidate_on_turn_start,
    on_turn_end=_execution_control_on_turn_end,
    on_end=_execution_control_on_end,
    emits=("repair_ledger_recorded",),
)
register_loop_hook(execution_control)


# ─────────────────────────────────────────────
# critical_rules_reminder —— 每 20 轮轻量 health tick（3 问）
# ─────────────────────────────────────────────
# 设计动机：LLM 在长对话中对 system prompt 的注意力会被稀释。原"每 8 轮强制四项自查
# + 灌 KB dead_end"已降级（2026-06-18）：① 改为每 20 轮只问 3 件事的轻量 health tick；
# ② KB dead_end 不再每轮灌，改为 turn 1 briefing 一次 + 事件触发（repeated_error 升级 /
# route 冲突 / 连续无进度），避免旧死路反复污染上下文。强制纠偏交给 strategic_review 的
# 事件触发（连续失败 / 5 轮无里程碑 / route 冲突 / 每 40 轮路线复盘）。

_HEALTH_TICK_INTERVAL = 20   # 轻量 health tick 间隔（2026-06-18 由原每 8 轮强制自查降级而来）

# 技术词提取：ASCII 词 ≥3 字符，排除英文常用虚词
_TECH_WORD_RE = _re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")
_STOP_WORDS = {"and", "the", "for", "with", "not", "are", "can", "has", "its",
               "was", "use", "using", "must", "this", "that", "from", "into",
               "all", "any", "when", "will", "should", "than", "then", "them"}


def _filter_relevant_dead_ends(dead_ends: list, messages: list) -> list:
    """按任务文本的技术词重叠过滤 dead_end，挡掉无关项目的死路噪声。

    任务词取首条 user 消息（含 experiment_focus / prereg 关键词）。
    无法取到任务词时不过滤（宁多勿漏）。
    """
    task_text = ""
    for m in messages:
        if getattr(m, "role", "") == "user":
            task_text = m.content or ""
            break
    task_words = {w.lower() for w in _TECH_WORD_RE.findall(task_text)} - _STOP_WORDS
    if not task_words:
        return dead_ends
    relevant = []
    for r in dead_ends:
        text = " ".join(str(r.get(k, "")) for k in
                        ("claim_text", "content", "dont_repeat_reason"))
        claim_words = {w.lower() for w in _TECH_WORD_RE.findall(text)} - _STOP_WORDS
        if task_words & claim_words:
            relevant.append(r)
    return relevant


def _dead_end_block(state, messages) -> str:
    """查 KB 相关 dead_end，格式化为注入块（无则 ""）。仅事件触发用：
    turn 1 briefing / repeated_error 升级 / route 冲突 / 连续无进度——平时不灌，避免旧死路污染上下文。
    相关性过滤（按任务技术词）挡掉无关项目的死路噪声。"""
    try:
        all_claims = state.list_kb("claims")
        dead_ends = [r for r in all_claims
                     if r.get("claim_type") == "dead_end"
                     and r.get("status") not in ("refuted", "superseded", "abandoned")]
        if dead_ends:
            dead_ends = _filter_relevant_dead_ends(dead_ends, messages)
        if not dead_ends:
            return ""
        lines = ["⛔ **KB 相关死路（禁止重走）**："]
        for r in dead_ends[-5:]:
            reason = r.get("dont_repeat_reason") or r.get("content", "")[:120]
            lines.append(f"  - {reason.strip()}")
        return "\n\n" + "\n".join(lines)
    except Exception:
        return ""


def _critical_rules_reminder_on_turn_start(ctx: HookContext) -> list | None:
    # turn 1：briefing 一次性注入相关 KB 死路（之后只在事件触发时查，不每轮灌）
    if ctx.turn == 1:
        block = _dead_end_block(ctx.state, ctx.messages)
        return [_sys("[briefing] 开工前——注意 KB 已记录的相关死路，勿重走。" + block)] if block else None
    # 轻量 health tick：只问 3 件事，不强制自查、不灌 dead_end（详细纪律见 SKILL）
    if ctx.turn % _HEALTH_TICK_INTERVAL != 0:
        return None
    return [_sys(
        f"[health_tick] 简单自检（每 {_HEALTH_TICK_INTERVAL} 轮）：\n"
        "① 当前官方构建路线走到第几步？\n"
        "② 上一个里程碑是什么？下一个目标里程碑是什么？\n"
        "③ 是否在重复某条已知 dead_end？"
    )]


def _critical_rules_reminder_candidate_on_turn_start(ctx: HookContext) -> list | None:
    result = _critical_rules_reminder_on_turn_start(ctx)
    # Turn-1 dead-end briefing is task orientation, not a mid-run correction.
    if ctx.turn == 1:
        return result
    _supervisor_offer_result(ctx, source="health_tick", priority=50, result=result)
    return None


critical_rules_reminder = LoopHook(
    name="critical_rules_reminder",
    description="每 20 轮轻量 health tick（3 问）；KB dead_end 改为 briefing 一次 + 事件触发，不再每轮灌",
    on_turn_start=_critical_rules_reminder_candidate_on_turn_start,
)
register_loop_hook(critical_rules_reminder)


# ─────────────────────────────────────────────
# plan_before_compile —— 首次编译前强制规划检测
# ─────────────────────────────────────────────
# 设计动机：agent 进入任务后第一件事往往是扫描已有脚本并直接复用，
# 跳过"查官方文档 → 写构建计划"这一关键步骤。
# 本 hook 检测"scratchpad 为空时执行编译命令"这一模式并上报规划候选。

_PLAN_WARNED_KEY = "_plan_warned"

def _plan_before_compile_on_turn_end(ctx: HookContext) -> list | None:
    if _is_operational_run(ctx.state):
        return None
    hook_state = ctx.state.hook_state
    if hook_state.get(_PLAN_WARNED_KEY):
        return None  # 只警告一次

    scratchpad_empty = not ctx.state.scratchpad

    for record in ctx.tool_call_records:
        if record.get("name") not in _BASH_TOOL_NAMES:
            continue
        cmd = (record.get("result") or {}).get("cmd", "")
        if scratchpad_empty and _is_build_cmd(cmd):
            hook_state[_PLAN_WARNED_KEY] = True
            # 知识正本在 SKILL §Diagnostic Discipline D4
            _supervisor_offer(
                ctx, source="plan_before_compile", priority=10,
                dedupe_key="first-build-without-plan",
                content=(
                    "[plan_before_compile] ⚠️ scratchpad 为空就执行了编译。"
                    "**停下，按 SKILL §Diagnostic Discipline D4 补做**："
                    "web_search 官方构建方法 → scratchpad 写阶段计划，再继续。"
                ),
            )
            return None
    return None


plan_before_compile = LoopHook(
    name="plan_before_compile",
    description="首次编译前 scratchpad 为空时上报规划候选，交由 execution_supervisor 仲裁",
    on_turn_end=_plan_before_compile_on_turn_end,
)
register_loop_hook(plan_before_compile)


# ─────────────────────────────────────────────
# task_briefing —— 用声明的任务正文输入做 KB 检索的 turn-1 briefing
# ─────────────────────────────────────────────
# builtin pre_run_briefing 的 query 优先级是 node_inputs.research_question >
# harness.kb_query。Experiment 的任务正文键由 task_prose_inputs.py 的同一份
# 节点声明决定；不能让 briefing 与 termination anchor 各自猜字段名。

def _task_briefing_on_turn_start(ctx: HookContext) -> list | None:
    if ctx.turn != 1:
        return None

    freeze_initial_task_prose_source(ctx.state)
    inputs = ctx.state.hook_state.get("node_inputs") or {}
    if inputs.get("research_question"):
        # Explicit Core-owned briefing exception: research_question feeds the
        # builtin retrieval hook, but is not a declared task-prose source and
        # never licenses an expected-termination anchor here.
        return None   # builtin briefing 已用任务级 query，无需补

    source = first_task_prose_source(inputs)
    focus = source.text if source is not None else ""
    if not focus:
        return None

    query = focus[:300]
    try:
        from core.recall import recall, render_briefing
        result = recall(ctx.state, query, k_per_category=5, min_cosine=0.7)
    except Exception as e:
        # 写 transcript（不只 log.debug）：静默失效曾导致 briefing 死了 6 天没人发现
        try:
            ctx.state.append_transcript(
                "task_briefing_skipped", turn=ctx.turn,
                reason=f"recall 异常: {type(e).__name__}: {e}"[:200])
        except Exception:
            pass
        return None

    if result.total_items() == 0:
        try:
            ctx.state.append_transcript(
                "task_briefing_skipped", turn=ctx.turn, reason="recall 返回 0 条")
        except Exception:
            pass
        return None   # 无相关历史，不注入空 briefing（builtin 那条已经够了）

    try:
        ctx.state.append_transcript(
            "task_briefing_injected",
            turn=ctx.turn,
            query_preview=query[:200],
            total_items=result.total_items(),
        )
    except Exception:
        pass

    md = render_briefing(result, query=query)
    return [_sys(
        "📚 **任务内容 BRIEFING（节点级）—— 基于声明的任务正文检索 KB**\n\n" + md
    )]


task_briefing = LoopHook(
    name="task_briefing",
    description="turn-1 用声明的任务正文做 KB 检索补充 briefing（builtin 的静态 kb_query 检索不到任务内容）",
    on_turn_start=_task_briefing_on_turn_start,
)
register_loop_hook(task_briefing)


# ─────────────────────────────────────────────
# prereg_binding_briefing —— 开局说清 prereg 绑定或待分配状态
# ─────────────────────────────────────────────
# 活体里 prereg 绑定失败要到第一次科学执行的 preflight 才告知，之前的几十轮都在为注定
# 执行不了的参数做准备，还出现过把 scope 改判为 operation 来绕开的倾向。
# 只在 prereg 是本轮明确声明的（声明的不存在 / 声明的有未冻结修订）时登记派发阻塞。
# 调用方未提供 typed prereg_assignment 时，catalog 只是 non-authorizing
# observation：hook 展示精确候选供父编排重派，不在 child 内猜选，也不因为“看见候选”
# 就登记 blocker。

def _prereg_binding_briefing_on_turn_start(ctx: HookContext) -> list | None:
    if ctx.turn != 1:
        return None
    state = ctx.state
    try:
        try:
            from tools.preflight import _register_prereg_dispatch_blocker
            from tools.run_contract import load_run_contract
        except ImportError:
            from .tools.preflight import _register_prereg_dispatch_blocker
            from .tools.run_contract import load_run_contract
        contract = load_run_contract(state)
    except Exception:
        return None
    inputs = state.hook_state.get("node_inputs") or {}
    declared_id = str(inputs.get("prereg_artifact_id") or "").strip()
    assignment = contract.get("prereg_assignment") or {}
    visibility = contract.get("prereg_visibility_witness") or {}
    observed_candidates: list[str] = []
    if isinstance(visibility, dict):
        for item in visibility.get("frozen") or []:
            if not isinstance(item, dict):
                continue
            artifact_id = str(item.get("artifact_id") or "").strip()
            version = item.get("version")
            content_hash = str(item.get("content_hash") or "").strip()
            if artifact_id and isinstance(version, int) and content_hash:
                observed_candidates.append(
                    f"{artifact_id}@v{version} (sha256:{content_hash})"
                )
        for item in visibility.get("pending") or []:
            if not isinstance(item, dict):
                continue
            artifact_id = str(item.get("artifact_id") or "").strip()
            version = item.get("latest_frozen_version")
            content_hash = str(
                item.get("latest_frozen_content_hash") or ""
            ).strip()
            draft_version = item.get("draft_version")
            if artifact_id and isinstance(version, int) and content_hash:
                observed_candidates.append(
                    f"{artifact_id}@v{version} (sha256:{content_hash}; "
                    f"unfrozen draft v{draft_version} also visible)"
                )
    registered = False
    if contract.get("contract_source") == "declared_prereg_not_found":
        kind = "declared_prereg_not_found"
        missing_id = str(contract.get("declared_prereg_id") or declared_id)
        reason = f"node_inputs.prereg_artifact_id={missing_id!r} 指向的预注册不存在或尚未冻结"
        _register_prereg_dispatch_blocker(state, kind=kind, declared_id=missing_id)
        registered = True
    elif declared_id and contract.get("pending_amendments"):
        kind = "prereg_amendment_pending"
        pending = [item for item in contract.get("pending_amendments") or []
                   if isinstance(item, dict)]
        reason = "声明的预注册有未冻结的修订草稿，本轮没有合法的绑定版本：" + "；".join(
            f"{item.get('artifact_id')}（草稿 v{item.get('draft_version')}，"
            f"最近冻结版 v{item.get('latest_frozen_version')}）" for item in pending)
        _register_prereg_dispatch_blocker(
            state, kind=kind, candidates=[str(item.get("artifact_id")) for item in pending])
        registered = True
    elif (
        isinstance(assignment, dict)
        and assignment.get("kind") == "pending"
        and observed_candidates
    ):
        kind = "prereg_assignment_pending"
        reason = (
            "调用方未提供 typed prereg_assignment；当前 catalog 只观察到"
            "下列候选，它们不构成本 run 的绑定或执行授权："
            + "、".join(observed_candidates)
        )
    elif contract.get("ambiguous_preregs"):
        # Legacy contract compatibility only. New v2 contracts expose a
        # pending assignment plus prereg_visibility_witness instead.
        kind = "ambiguous_preregs"
        reason = ("项目里有多份冻结预注册，本轮没有在 node_inputs.prereg_artifact_id 里指名："
                  + "、".join(str(item) for item in contract["ambiguous_preregs"]))
    elif contract.get("pending_amendments"):
        # 没声明、项目里唯一那份冻结 prereg 挂着修订草稿：preflight 仍会挡科学执行，这里和
        # 有歧义时一样只提示、不登记（第三会话复审 0914c K2 P3-1）。
        kind = "prereg_amendment_pending"
        pending = [item for item in contract.get("pending_amendments") or []
                   if isinstance(item, dict)]
        reason = "项目里的冻结预注册挂着未冻结的修订草稿，本轮没有指名绑定版本：" + "；".join(
            f"{item.get('artifact_id')}（草稿 v{item.get('draft_version')}，"
            f"最近冻结版 v{item.get('latest_frozen_version')}）" for item in pending)
    else:
        return None
    try:
        state.append_transcript(
            "prereg_binding_briefing_injected", turn=ctx.turn, kind=kind,
            blocker_registered=registered)
    except Exception:
        pass
    if registered:
        body = ("已向 orchestrator 登记派发阻塞：请它在 node_inputs 里给出一份已冻结的 "
                "prereg_artifact_id（有修订草稿时同时给 prereg_version）后重派。本轮执行不了"
                "预注册的科学参数：保存已有的有效工作后，用 `report_blocker` 如实收尾。")
    else:
        body = ("如果本任务要按预注册执行科学参数，执行前会被派发阻塞挡住：先用 `report_blocker` "
                "请 orchestrator 指名 prereg_artifact_id；本任务不消费预注册（operation 或探索性"
                "运行）时照常进行。")
        if kind == "prereg_assignment_pending":
            body = (
                "本 child 不会自动选择候选，本提示也不登记 blocker。"
                "如果任务需要 prereg 权威，应由 dispatching parent 新建 run，"
                "并提供 typed prereg_assignment=bound 的精确 id/version/hash；"
                "若上游确认本 run 无 governing prereg，则重派时显式提供 typed none。"
            )
    # 只提示的两格对不消费 prereg 的 operation 任务是噪音，标题不说「失败」（0914c K2 P3-2）。
    title = (f"⚠️ **预注册绑定失败**（{kind}）" if registered
             else f"提示：项目预注册状态需要留意，本轮未指名（{kind}）")
    if registered:
        guardrail = (
            "**不要为了绕开这道门把 scope 改判为 operation**——prereg "
            "绑定失败形成 blocker，不降格。"
        )
    elif kind == "prereg_assignment_pending":
        guardrail = (
            "候选可见性和 typed none 都不决定 operation/scientific；分类仍按本 run "
            "的认知目的，且不得把候选观察冒充绑定授权。"
        )
    else:
        guardrail = (
            "若后续确认本任务必须消费 prereg，不得为了绕开绑定问题把 scope "
            "改判为 operation。"
        )
    return [_sys(
        f"{title}：{reason}。\n{body}\n"
        f"{guardrail}"
    )]


prereg_binding_briefing = LoopHook(
    name="prereg_binding_briefing",
    description="turn-1 读 run 契约；说清 prereg 绑定失败或待分配的非授权候选观察（不改判 scope）",
    on_turn_start=_prereg_binding_briefing_on_turn_start,
)
register_loop_hook(prereg_binding_briefing)


# ─────────────────────────────────────────────
# data_source_reachability_preflight —— turn-1 数据源可达性预检
# ─────────────────────────────────────────────
# 设计动机（2026-08-30 airsea 复盘）：冻结 prereg 的「数据源与许可前提」小节里写
# 着 downloads.psl.noaa.gov，而运行时 egress 白名单只放行 pypi/github/zenodo，
# 缺口跑到第 43 轮真正下载时才暴露。这里在 turn 1 就用与 egress 代理**同源**的
# 判据（host == domain or host.endswith("." + domain)）机械比对，**只在有缺口时**
# 注入一段 ≤6 行的通知；全部可达 → 零注入零 prompt 成本。解析不出小节/主机同样
# 静默。本 hook 只注入信息，不阻断任何工具调用。

_DATA_SOURCE_PREFLIGHT_KEY = "_data_source_reachability_checked"

# 显式排除的"长得像域名"的文件扩展名与数据格式后缀。注意 .nc/.sh/.in/.md 同时
# 是真实 ccTLD，不排除就会把 sst.mnmean.nc 误判成主机 —— 宁可漏报不可误报。
_NON_HOST_SUFFIXES = frozenset("""
nc nc4 cdf cdl netcdf grb grb2 grib grib2 hdf hdf5 h5 zarr npy npz mat sav
csv tsv txt dat bin log out err json yaml yml toml ini cfg conf nml namelist
md rst html htm css js xml pdf png jpg jpeg tif tiff svg gif mp4 avi
py sh bash csh zsh pl rb jl f f90 f77 c cc cpp cxx h hpp o so a exe whl
zip tar gz tgz xz bz2 zst rar iso img deb rpm lock bak tmp swp orig rej
db sql sqlite parquet feather pt pth ckpt pkl safetensors ipynb docx xlsx pptx
""".split())

# 裸 token 扫描：至少两段、字符集 [a-z0-9.-]；左侧 lookbehind 含 "/" —— URL 路径
# 段（.../ncep.reanalysis/...）不算主机，主机由 _URL_HOST_RE 单独负责。
_HOST_TOKEN_RE = re.compile(
    r"(?<![0-9a-z._/-])([a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)"
)
_URL_HOST_RE = re.compile(r"https?://([^\s/?#\"'<>）)\]，、；]+)", re.I)


def _looks_like_host(host: str) -> bool:
    """Conservative hostname test: prefer missing a host over inventing one."""
    if not host or len(host) < 4 or ".." in host:
        return False
    labels = host.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return False
    tld = labels[-1]
    if not tld.isalpha() or not 2 <= len(tld) <= 24:
        return False          # 版本号 v1.2.3 / 分辨率 0.25 的尾段不是字母
    if tld in _NON_HOST_SUFFIXES:
        return False          # sst.mnmean.nc、requirements.txt 之类
    if all(label.isdigit() for label in labels[:-1]):
        return False
    return True


def _frozen_prereg_content(state: Any) -> str:
    """本 run 能读到的冻结 pre_registration 正文；读不到返回空串。"""
    try:
        entries = state.list_artifacts("pre_registration") or []
    except Exception:
        return ""
    for entry in entries:
        artifact_id = str((entry or {}).get("id") or "")
        if not artifact_id:
            continue
        try:
            record = state.read_artifact(artifact_id) or {}
        except Exception:
            continue
        metadata = record.get("metadata")
        if isinstance(metadata, dict) and metadata.get("frozen"):
            return str(record.get("content") or "")
        try:
            latest = state.latest_frozen_artifact(artifact_id)
        except Exception:
            latest = None
        if isinstance(latest, dict):
            return str(latest.get("content") or "")
    return ""


def _data_source_section(text: str) -> str:
    """定位「数据源/资源 + 许可/前提」小节正文（标题大小写与全半角宽容）。"""
    lines = text.splitlines()
    start = -1
    level = 0
    for index, line in enumerate(lines):
        matched = re.match(r"^(#{1,6})\s*(.+?)\s*$", line)
        if not matched:
            continue
        title = (matched.group(2)
                 .replace("（", "(").replace("）", ")")
                 .replace("　", "").replace(" ", "").lower())
        subject = any(word in title for word in ("数据源", "数据来源", "资源", "datasource", "datasources"))
        premise = any(word in title for word in ("许可", "前提", "licen"))
        if subject and premise:
            start = index + 1
            level = len(matched.group(1))
            break
    if start < 0:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        heading = re.match(r"^(#{1,6})\s", line)
        if heading and len(heading.group(1)) <= level:
            break
        body.append(line)
    return "\n".join(body)


def _extract_declared_hosts(section: str) -> list[str]:
    candidates: list[str] = []
    for matched in _URL_HOST_RE.finditer(section):
        raw = matched.group(1).lower().split("@")[-1].split(":")[0]
        candidates.append(raw)
    for matched in _HOST_TOKEN_RE.finditer(section.lower()):
        candidates.append(matched.group(1))
    hosts: list[str] = []
    for candidate in candidates:
        host = candidate.strip().strip(".").lower()
        if _looks_like_host(host) and host not in hosts:
            hosts.append(host)
    return hosts


# ── 域名风险标注（只读常量表；**仅供 owner 目视判断，绝不用于自动放行**）──────
# 命中只是往注入文案里多写一个机构名，让 owner 一眼能批。本模块没有任何据此
# 改写 HARNESS_SANDBOX_EGRESS_ALLOWLIST / os.environ 的代码路径，测试钉死。
_KNOWN_RESEARCH_DOMAIN_LABELS: tuple[tuple[str, str], ...] = (
    ("noaa", "NOAA 美国海洋与大气管理局"),
    ("nasa", "NASA 美国航空航天局"),
    ("ncar", "NCAR 美国国家大气研究中心"),
    ("ucar", "UCAR 大学大气研究联盟"),
    ("ceda", "CEDA 英国环境数据分析中心"),
    ("ecmwf", "ECMWF 欧洲中期天气预报中心"),
    ("copernicus", "Copernicus 欧盟哥白尼计划"),
    ("esgf", "ESGF 地球系统网格联盟"),
    ("usgs", "USGS 美国地质调查局"),
    ("nsf", "NSF 美国国家科学基金会"),
)


def _host_tld_class(host: str) -> str:
    """TLD 类别：.gov / .edu / .org / .ac.xx / 其他（可机械判定）。"""
    labels = str(host).strip().strip(".").lower().split(".")
    if len(labels) >= 2 and labels[-2] == "ac":
        return f".ac.{labels[-1]}"          # ac.uk / ac.jp / ac.cn
    tld = labels[-1] if labels else ""
    return f".{tld}" if tld in ("gov", "edu", "org") else "其他"


def _host_risk_note(host: str) -> str:
    """`host（.gov／NOAA …）` —— 给人的判断辅助，最终仍由 owner 批准。

    机构命中按**域名标签**精确匹配（标签再按 '-' 切分），不做裸子串匹配 ——
    否则 "transfer.example.org" 会因为含 "nsf" 被误标成 NSF。
    """
    marks = [_host_tld_class(host)]
    segments = {
        segment
        for label in str(host).strip().strip(".").lower().split(".")
        for segment in label.split("-")
    }
    for label, institution in _KNOWN_RESEARCH_DOMAIN_LABELS:
        if label in segments:
            marks.append(institution)
            break
    return f"{host}（{'／'.join(marks)}）"


def _data_source_reachability_on_turn_start(ctx: HookContext) -> list | None:
    if ctx.turn != 1:
        return None
    state = ctx.state
    try:
        if state.hook_state.get(_DATA_SOURCE_PREFLIGHT_KEY):
            return None
    except Exception:
        return None

    section = _data_source_section(_frozen_prereg_content(state))
    hosts = _extract_declared_hosts(section) if section.strip() else []
    if not hosts:
        return None                      # 无 prereg / 无小节 / 提取为空 → 静默

    try:
        from tools.resource_fetch import _egress_access_decision
        policy = _egress_access_decision(state)["policy"]
    except Exception:
        return None
    entries = [str(item) for item in (policy.get("entries") or [])]
    missing = [
        host for host in hosts
        if not _egress_access_decision(state, host)["allowed"]
    ]

    try:
        state.hook_state[_DATA_SOURCE_PREFLIGHT_KEY] = True
        state.append_transcript(
            "data_source_reachability_preflight", turn=ctx.turn,
            declared_hosts=hosts, unreachable_hosts=missing,
            egress_source=str(policy.get("source") or ""),
            per_run_granted_hosts=list(
                policy.get("per_run_granted_hosts") or []
            ),
        )
    except Exception:
        pass

    if not missing:
        return None                      # 全部可达 → 零注入

    allowed = ", ".join(entries[:6]) + ("…" if len(entries) > 6 else "")
    annotated = ", ".join(_host_risk_note(host) for host in missing)
    requests = "；".join(
        f'request_network_access(host="{host}", reason="获取冻结 prereg 声明的数据源", '
        f'what_for="{host}")'
        for host in missing
    )
    proposed = ",".join(dict.fromkeys([*entries, *missing]))
    return [_sys(
        "🚧 **数据源可达性预检（turn 1，机械比对冻结 prereg 的数据源小节）**\n"
        f"当前 egress 够不到：{annotated}（判据与 fetch_resource 联网前核对完全相同）\n"
        f"部署白名单来源：{policy.get('source') or '未知'}｜已放行：{allowed}\n"
        "⚠️ 这是**平台配置限制**，不是「数据不存在」—— 这些源公网可下载。不要绕开，不要因此改小科学设计。\n"
        "**动手下载之前**逐个申请本 run 精确主机授权，不要拿失败下载试探：\n"
        f"  {requests}\n"
        "用户允许后原样重试 `fetch_resource`；拒绝或未答就换合法路径或如实报卡住。\n"
        "需要长期放行时由部署方复制这行配置并重启："
        f" `HARNESS_SANDBOX_EGRESS_ALLOWLIST={proposed}`；带凭据的数据源仍需另行解决账号前提。\n"
        "换替代源或按缺变量走正式修订只是**最后手段**，仅当授权、长期配置和账号前提都不成立；"
        "理由必须是科学的，不能是「平台不让我拿」。\n"
        "括号里的 TLD／机构标注只是给 owner 的判断辅助，**最终由 owner 批准**，本 hook 不会自动放行任何域名。\n"
        "若该数据只影响部分研究问题/步骤，先推进不依赖它的部分，**不得因单个数据源停掉整个 run**。"
    )]


data_source_reachability_preflight = LoopHook(
    name="data_source_reachability_preflight",
    description="turn-1 复用 fetch_resource 的部署后缀/本 run 精确授权判定；仅在有缺口时注入申请授权指引（不阻断）",
    on_turn_start=_data_source_reachability_on_turn_start,
)
register_loop_hook(data_source_reachability_preflight)


# ─────────────────────────────────────────────
# foreign_owner_blocker_ask_nudge —— 把 blocker 判给别人前先问一句
# ─────────────────────────────────────────────
# 设计动机：「`suggested_owner` 非 experiment 的阻塞先问一句再 report_blocker」本来
# 是条硬约束，写进 rules 要花 ~35 token 且每个 run 都付（AGENTS.md：硬约束落确定性
# 机制，提示词只做操作指引）。做成 hook 后**不触发就零字**：观测到本 run 调了
# `report_blocker` 且显式判给了别人、而此前从未发起普通人工提问或能力授权时，才注入
# 一条 ≤3 行提醒。egress 白名单缺域名、宿主缺隔离后端（bwrap/Landlock）这类问题 owner 几秒
# 就能解，不该默默停车。
#
# 三条边界：
#   1. **绝不阻断** —— 只在 on_turn_end 注入，不拦 report_blocker、不改其返回值。
#   2. **字段缺失即不触发** —— harness.yaml 的 scheduler 路径规则要求那类 blocker 用
#      `human_action='not_applicable'` 且**不带** suggested_owner；缺字段/空串一律放过，
#      免得和"不得为默认路径询问人"的规则打架。
#   3. 归属含 "experiment" 一律当自己人（宁可漏提醒不可误提醒）。

_FOREIGN_OWNER_NUDGE_KEY = "_foreign_owner_blocker_nudged"
_HUMAN_ASK_TOOLS = frozenset({"request_human_input", "request_network_access"})


def _foreign_blocker_owner(record: Any) -> str:
    """本条记录是"显式判给 experiment 之外的 owner"的 report_blocker → 返回该 owner。"""
    if not isinstance(record, dict) or record.get("name") != "report_blocker":
        return ""
    args = record.get("args")
    if not isinstance(args, dict) or "suggested_owner" not in args:
        return ""                      # 字段不存在 → 不触发（scheduler 路径那类）
    owner = str(args.get("suggested_owner") or "").strip()
    if not owner or "experiment" in owner.lower():
        return ""                      # 空串 / 判给自己 → 不触发
    return owner


def _asked_human_this_run(state: Any, records: list) -> bool:
    """本 run 是否问过人：先看本轮记录，再回扫 transcript（问人会 pause，那一轮
    的 on_turn_end 未必跑过，所以不能只靠 hook_state 累计）。"""
    for record in records:
        if isinstance(record, dict) and record.get("name") in _HUMAN_ASK_TOOLS:
            return True
    try:
        path = Path(state.transcript_path)
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8", errors="ignore") as stream:
            for line in stream:
                if not any(name in line for name in _HUMAN_ASK_TOOLS):
                    continue           # 便宜的预筛，避免逐行 json.loads
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if (event.get("event") == "tool_call"
                        and event.get("name") in _HUMAN_ASK_TOOLS):
                    return True
    except Exception:
        return True                    # 读不到就当问过 —— 宁可不提醒
    return False


def _foreign_owner_blocker_nudge_on_turn_end(ctx: HookContext) -> list | None:
    records = ctx.tool_call_records or []
    owner = ""
    for record in records:
        owner = _foreign_blocker_owner(record)
        if owner:
            break
    if not owner:
        return None                    # 没报 blocker / 没判给别人 → 零注入

    state = ctx.state
    try:
        if state.hook_state.get(_FOREIGN_OWNER_NUDGE_KEY):
            return None                # 同一 run 最多提醒一次
    except Exception:
        return None

    if _asked_human_this_run(state, records):
        return None                    # 已经问过人 → 零注入

    try:
        state.hook_state[_FOREIGN_OWNER_NUDGE_KEY] = True
        state.append_transcript(
            "foreign_owner_blocker_ask_nudge", turn=ctx.turn, suggested_owner=owner,
        )
    except Exception:
        pass

    return [_sys(
        f"❓ 这条阻塞你判给了 **{owner}**，但本 run 还没问过 owner。\n"
        "很多这类问题 owner 几秒就能解（宿主缺隔离后端、"
        "egress 白名单缺域名）。\n"
        "确属对方职责且你已无法推进 → 保持 blocker；否则先用 `request_human_input` "
        "问一句（带 options + 推荐项 + 无应答默认）。"
    )]


foreign_owner_blocker_ask_nudge = LoopHook(
    name="foreign_owner_blocker_ask_nudge",
    description="report_blocker 显式判给 experiment 之外的 owner 而本 run 从未发起普通人工提问或能力授权时，注入一次「先问一句」提醒（不阻断，不改返回值）",
    on_turn_end=_foreign_owner_blocker_nudge_on_turn_end,
)
register_loop_hook(foreign_owner_blocker_ask_nudge)


# ─────────────────────────────────────────────
# platform_limit_ask_nudge —— 撞上平台限制时请求拆墙，而不是改设计/停车
# ─────────────────────────────────────────────
# 设计动机（2026-08-30 airsea 复盘）：冻结预注册 v17→v18 的修订说明原文写着
# 「UOHC/TCHP 的所有公开源（EN4/SODA3/NOAA PSL/NCAR RDA）均被平台 egress
# allowlist 挡死（实测 403）」，于是把 Q1 的核心对照从「独立于 UOHC/TCHP」降级
# 为「独立于绝对 SST」—— **那些数据任何人用浏览器都能下载**。一个配置缺口把一个
# 研究问题的科学对照改小了，比"停车"更隐蔽也更严重。
#
# egress 白名单只是其中一种墙。这里观测本 run 的**工具返回**，识别"平台限制形状"
# 的失败（白名单/沙箱不可用/walltime/文件大小/路径能力），一次性注入同款「请求拆墙」
# 指引。平台施加的人为限制不是科学事实：正确反应是说清哪道墙 + 需要什么 + 为什么 +
# 风险判断 + 现成操作，请 owner 拆；不是绕开改科学设计，也不是停掉整个 run。
#
# 三条边界（与上面两个 hook 同构）：
#   1. **只在失败的返回上匹配** —— 成功路径零成本，散文里提到 "egress" 不触发。
#   2. **同 run 最多注入一次**，且只在 on_turn_end 注入，不阻断任何工具。
#   3. **不改任何工具返回值** —— 只读 records，测试用 json dump 前后对拍钉死。

_PLATFORM_LIMIT_NUDGE_KEY = "_platform_limit_nudged"

# (类别, 该类别的判定标记)。标记取自平台**真实**产出的串：
# core/isolation 的 _resolve_auto 在原生后端守不住写边界时抛的 IsolationContractError
# （"the <平台> backend cannot enforce the write boundary (I1) …" / "no native isolation
# backend for …"），经受管咽喉 spawn_and_wait 以 spawn_failed 原文带进工具返回；
# core/sandbox.py 的 SandboxUnavailable("execution boundary unavailable: …") 只由
# require_available() 抛，而它眼下没有调用方（#775），不作标记；
# "HARNESS_SANDBOX_LIMIT <violation>"：pids 一种由本节点 resource_manager 的守卫脚本写出，
# walltime / storage 两种原先出自 Docker 年代的 deploy/sandbox（已删），眼下找不到产出方，
# 保留识别只是不漏报；
# nodes/experiment/tools/safe_bash.py 的 reason="path_capability_required"。
_PLATFORM_LIMIT_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("egress 白名单", (
        "egress allowlist", "egress_allowlist", "egress_blocked", "egress_denied",
        "not in allowlist", "403 forbidden", "http 403",
        "status_code: 403", "egress",
    )),
    # Docker 准入/内存池那批串（sandbox admission、reserved=、docker memory…）随
    # 71f2e20c 删掉 Docker 执行器后已无产出方（#775），只留现在真会出现的形状。
    ("沙箱不可用", (
        "cannot enforce the write boundary", "no native isolation backend",
    )),
    ("walltime / 超时上限", (
        "harness_sandbox_limit walltime", "harness_sandbox_limit output",
        "walltime_exceeded", "walltime exceeded",
    )),
    ("文件大小上限", (
        "harness_sandbox_limit storage", "max_bytes", "file too large",
    )),
    ("路径能力 / 可写根", (
        "path_capability_required", "approved_write_root",
        "not_scheduler_usable", "write root not declared",
    )),
)


def _looks_like_failed_result(result: Any) -> bool:
    """保守的失败判据：拿不准一律当成功（宁可漏提醒，不可误提醒）。"""
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").strip().lower()
    if status and status not in ("success", "ok", "completed", "done"):
        return True
    if result.get("error") or result.get("failed") or result.get("blocked"):
        return True
    for key in ("returncode", "exit_code", "rc"):
        if key in result:
            try:
                if int(result[key]) != 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _platform_limit_category(record: Any) -> tuple[str, str]:
    """(类别, 工具名)；不是平台限制形状的失败返回 ("", "")。"""
    if not isinstance(record, dict):
        return "", ""
    result = record.get("result")
    if not _looks_like_failed_result(result):
        return "", ""            # 成功 / 非 dict 返回 → 零成本
    try:
        text = json.dumps(result, ensure_ascii=False, default=str).lower()
    except Exception:
        return "", ""
    for category, markers in _PLATFORM_LIMIT_SIGNALS:
        if any(marker in text for marker in markers):
            return category, str(record.get("name") or "")
    return "", ""


def _platform_limit_nudge_on_turn_end(ctx: HookContext) -> list | None:
    category = tool_name = ""
    for record in (ctx.tool_call_records or []):
        category, tool_name = _platform_limit_category(record)
        if category:
            break
    if not category:
        return None                    # 没撞墙 → 零注入零 prompt 成本

    state = ctx.state
    try:
        if state.hook_state.get(_PLATFORM_LIMIT_NUDGE_KEY):
            return None                # 同一 run 最多注入一次
    except Exception:
        return None

    try:
        state.hook_state[_PLATFORM_LIMIT_NUDGE_KEY] = True
        state.append_transcript(
            "platform_limit_ask_nudge", turn=ctx.turn,
            limit_category=category, tool=tool_name,
        )
    except Exception:
        pass

    return [_sys(
        f"🧱 **这是平台限制，不是科学事实**（检测到：{category}；工具 `{tool_name}`）\n"
        "egress 白名单 / 沙箱不可用 / walltime / 文件大小 / 可写根 —— 都是**平台配置**挡的，"
        "现实世界拿得到、跑得动，owner 往往几秒就能拆（白名单加一个域名、给可写根加一条路径）。\n"
        "**不要绕开去改科学设计，也不要因此停掉整个 run。**\n"
        "用 `request_human_input` 一次问全：①是哪道墙（贴上面的原始报错）②需要什么"
        "（具体域名 / 配额值 / 路径）③为什么需要（对应 prereg 哪一条）④风险判断"
        "⑤**现成可执行的解除操作**（一行命令或一行配置）。\n"
        "必带 options + 推荐项 + 无应答默认。改替代方案或改预注册是最后手段，理由必须是科学的。\n"
        "若这道墙只挡住部分研究问题 / 步骤，先推进不依赖它的部分。"
    )]


platform_limit_ask_nudge = LoopHook(
    name="platform_limit_ask_nudge",
    description="工具返回呈现平台限制形状的失败（egress/沙箱不可用/walltime/大小/路径能力）时，注入一次「请求拆墙」指引（不阻断，不改返回值，同 run 一次）",
    on_turn_end=_platform_limit_nudge_on_turn_end,
)
register_loop_hook(platform_limit_ask_nudge)


# ─────────────────────────────────────────────
# path_role_convention —— 注入最小路径角色契约
# ─────────────────────────────────────────────
# 设计动机（2026-07-10 meiyu laptop pilot 审计）：实验物理 workdir 此前靠任务描述
# 显式传路径，未传时 agent 随手落在 ~/experiments/<name>。约定：
# experiment_root 只是逻辑容器，不作为可写白名单；实际写入必须落到
# source_worktree_root / source_patch_root / build_root / run_root。run_root 和
# build_root 都有 run-local 的隐式分配；managed_source_root 与 build_root 是
# 惰性角色，不创建空壳目录，但应在模型规划前公开，避免上游或模型先猜任务类型。
# 显式声明仍优先，外部共享盘不得靠默认值推断。

_PATH_ROLE_CONV_INJECTED_KEY = "_path_role_convention_injected"


def _path_role_convention_on_turn_start(ctx: HookContext) -> list | None:
    existing = collect_path_roles(ctx.state, include_runtime=False)
    by_name: dict[str, list] = {}
    for role in existing:
        by_name.setdefault(role.role, []).append(role)

    explicit_experiment = by_name.get("experiment_root", [])
    explicit_run = by_name.get("run_root", [])
    if explicit_experiment:
        experiment_root = Path(explicit_experiment[0].path)
    elif explicit_run:
        experiment_root = Path(explicit_run[0].path).parent
    else:
        try:
            experiment_root = experiment_output_dir(ctx.state)
        except ValueError:
            return None
    if experiment_root is None:
        return None

    run_root = Path(explicit_run[0].path) if explicit_run \
        else experiment_root / "runtime"
    role_state = ctx.state.hook_state.setdefault("path_roles", {})
    workspace_root = getattr(ctx.state, "workspace_root", None)
    is_owned_workspace = False
    if workspace_root is not None:
        try:
            is_owned_workspace = (
                Path(experiment_root).resolve(strict=False)
                == Path(workspace_root).resolve(strict=False))
        except (OSError, ValueError):
            is_owned_workspace = str(experiment_root) == str(workspace_root)
    if is_owned_workspace:
        # Core assigns this directory to Experiment as its Git workspace. It
        # remains writable for ordinary node files, but is never a job root or
        # a cleanup root. Immutable baseline descendants override this role.
        # workspace_root itself is injected from State by collect_path_roles().
        # Keeping a second hook_state copy gives it different provenance and
        # turns a legitimate baseline below this workspace into a conflict.
        role_state.pop("experiment_root", None)
        role_state.pop("workspace_root", None)
    else:
        role_state.setdefault("experiment_root", {
            "path": str(experiment_root),
            "writable": False,
            "container_only": True,
        })
    role_state.setdefault("run_root", {
        "path": str(run_root),
        "writable": True,
    })
    # Framework already owns isolated run-local source/build allocations.
    # Read them back from the canonical collector instead of copying defaults
    # into hook_state and creating a second authority source.
    runtime_roles = collect_path_roles(ctx.state, include_runtime=True)
    build_roots = sorted({
        role.path for role in runtime_roles if role.role == "build_root"
    })
    managed_source_roots = sorted({
        role.path for role in runtime_roles
        if role.role == "managed_source_root"
    })
    install_prefixes = sorted({
        str(Path(path) / "install") for path in build_roots
    })

    def _render_lazy_role(
        role_name: str,
        paths: list[str],
        purpose: str,
    ) -> str:
        if len(paths) == 1:
            return f"- `{role_name}`: `{paths[0]}/`（{purpose}；按需创建）\n"
        candidates = "\n".join(f"  - `{path}/`" for path in paths)
        return (
            f"- `{role_name}`：存在 {len(paths)} 个已授权候选（{purpose}）；"
            "执行时必须显式消歧：\n" + candidates + "\n"
        )

    if ctx.state.hook_state.get(_PATH_ROLE_CONV_INJECTED_KEY):
        return None
    ctx.state.hook_state[_PATH_ROLE_CONV_INJECTED_KEY] = True

    try:
        experiment_root.mkdir(parents=True, exist_ok=True)
        run_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        ctx.state.append_transcript(
            "path_role_convention_injected", turn=ctx.turn,
            experiment_root=str(experiment_root), run_root=str(run_root),
            managed_source_roots=managed_source_roots,
            build_roots=build_roots,
            explicit=bool(explicit_experiment or explicit_run),
            project_level=False)
    except Exception:
        pass

    scope_note = "run-local 节点输出（本 run 独立；跨 run 资产必须显式声明角色）"
    declaration_note = "显式声明或由显式 run_root 推导" \
        if explicit_experiment or explicit_run else scope_note
    root_line = (
        f"- `workspace_root`: `{workspace_root}/`（本节点 Git 工作目录；普通节点文件可写，"
        "但不是 run_root，不能作为 scheduler workdir 或清理根）\n"
        if is_owned_workspace else
        f"- `experiment_root`: `{experiment_root}/`（{declaration_note}；仅逻辑容器，不得直接写入）\n")
    return [_sys(
        "📁 **路径角色契约（path_role_convention hook）**\n\n" +
        root_line +
        f"- `run_root`: `{run_root}/`（可写；运行输入、日志、输出和 checkpoint）\n"
        + _render_lazy_role(
            "managed_source_root", managed_source_roots,
            "仅本 run 下载、解压源码",
        )
        + _render_lazy_role(
            "build_root", build_roots,
            "存放编译缓存与产物",
        )
        + _render_lazy_role(
            "install_prefix", install_prefixes,
            "对应 build_root/install；所有安装器均须显式定向到这里",
        )
        + "默认源码与构建目录分离：不得在 managed_source_root 或 source_baseline_root "
        "内编译；只有软件官方入口明确要求 in-tree build、冻结路线步骤声明 "
        "source_worktree_root 且该目录为独立可写 worktree 时，才允许在其中构建并"
        "保留 patch/diff 与产物证据。不得探索、复用或清理 project workspace，"
        "除非它已在 `path_roles` 中被显式声明。\n"
        + "`managed_source_root`、`source_baseline_root`、`source_worktree_root`、"
        "`source_patch_root`、`dependency_root` 均按实际活动声明，不要求空建；"
        "managed_source_root 与 build_root 是框架已有的惰性 run-local 分配；"
        "路线步骤使用对应 workdir_role 且省略 cwd 时，resolver 会选择上面给出的精确目录。"
        "项目 run 的源码、构建、安装和运行输出必须留在 project workspace 内。"
        "**构建输入还必须在你自己的角色目录内**：源码若来自工作区物料目录或别的节点"
        "留下的副本（例如编排层解压出来的那份），先取进 managed_source_root 再构建 ——"
        "那些位置不归你管，构建期间可能被改写，直接拿它当输入会让这次结果无法复现；"
        "取入时把来源路径与文件哈希记进 scratchpad 和 experiment_log。"
        "编译只使用上述 build_root；CMake 配置须传 -DCMAKE_INSTALL_PREFIX=<install_prefix>，"
        "其他构建/安装器也须尽可能用 PREFIX、DESTDIR、--prefix 等机制定向到 install_prefix；"
        "若修改源码必须先声明独立的"
        " source_worktree_root，并保存 patch/diff 记录。把最终角色路径写入"
        " scratchpad 和 experiment_log。\n\n"
        "⚠️ **路径角色是权限，只能来自框架 run-local 分配、fixture / 编排输入或人工批准。**"
        "artifact（含你自己保存的 `declared_route`、`pre_registration`）"
        "**不会**产生任何角色——在里面写 `source_path` / `build_dir` 既不会获得"
        "写权限，也不会把某棵树锁成只读。需要在未声明的目录下工作时，直接执行，"
        "工具会就该目录请求一次人工批准；批准后该目录在本 run 内可写、可清空内容，"
        "不必再逐条询问。"
    )]



path_role_convention = LoopHook(
    name="path_role_convention",
    description="turn-1 注入最小规范路径角色；外部 experiment_root 是容器，绑定 workspace 使用专用角色",
    on_turn_start=_path_role_convention_on_turn_start,
    emits=("path_role_convention_injected"),
)
register_loop_hook(path_role_convention)


# ─────────────────────────────────────────────
# turn_one_briefing —— 本节点开局提示合并成一条（收敛任务书 K11 第 2 条）
# ─────────────────────────────────────────────
# 第 1 轮本节点的四个 hook 各注入一条 system 消息，模型开局先读四段互不相干的提示。
# 这里按原顺序调用同样四个函数，把各自的消息拼成一条；每个函数的判定、副作用
# （路径角色写入 hook_state、prereg 派发阻塞登记、transcript 事件）都不变。
# path_role_convention 每轮都要跑（每轮补写 path_roles，只在第一次注入消息），所以
# 这里每轮都调，其他三个自己只在第 1 轮出消息。共享层 hook（project_orientation 等）不动。
_TURN_ONE_BRIEFING_PARTS = (
    ("path_role_convention", _path_role_convention_on_turn_start),
    ("task_briefing", _task_briefing_on_turn_start),
    ("prereg_binding_briefing", _prereg_binding_briefing_on_turn_start),
    ("data_source_reachability_preflight", _data_source_reachability_on_turn_start),
)


def _turn_one_briefing_on_turn_start(ctx: HookContext) -> list | None:
    parts: list[str] = []
    for name, part in _TURN_ONE_BRIEFING_PARTS:
        try:
            result = part(ctx)
        except Exception as exc:
            # 各 hook 原先由 core 逐个兜底；合并后一段失败不能吞掉其他几段。
            log.warning("turn_one_briefing 的 %s 失败：%s", name, exc)
            try:
                ctx.state.append_transcript(
                    "turn_one_briefing_part_failed", turn=ctx.turn, part=name,
                    error=f"{type(exc).__name__}: {exc}"[:200])
            except Exception:
                pass
            continue
        for message in result or []:
            content = str(getattr(message, "content", "") or "").strip()
            if content:
                parts.append(content)
    if not parts:
        return None
    return [_sys("\n\n---\n\n".join(parts))]


turn_one_briefing = LoopHook(
    name="turn_one_briefing",
    description=(
        "本节点开局提示合并成一条：路径角色契约、任务 KB briefing、prereg 绑定失败、"
        "数据源可达性预检（收敛任务书 K11）"
    ),
    on_turn_start=_turn_one_briefing_on_turn_start,
    emits=("path_role_convention_injected", "task_briefing_injected",
           "prereg_binding_briefing_injected", "data_source_reachability_preflight",
           "turn_one_briefing_part_failed", TASK_PROSE_INPUT_RECEIPT_EVENT),
)
register_loop_hook(turn_one_briefing)


# Python API compatibility for older local tests/extensions. The registered hook
# and emitted transcript use only the canonical name.
def _workdir_convention_on_turn_start(ctx: HookContext) -> list | None:
    return _path_role_convention_on_turn_start(ctx)


# ─────────────────────────────────────────────
# gpu_skill_injector —— 仅明确 NVIDIA CUDA 运行加载 CUDA 专属知识
# ─────────────────────────────────────────────
# GPU skill 是条件知识，不应污染所有 CPU/软件修复运行的上下文。优先读取编排器
# 明确提供的 requires_gpu；旧 fixture 没有该字段时，才在 prereg 文本中做保守识别。

def _gpu_backend_hint(state: Any) -> str | None:
    """Return a conservative accelerator hint without guessing a GPU backend."""
    texts: list[str] = []
    try:
        from tools.run_contract import load_run_contract
        contract = load_run_contract(state)
        texts.append(json.dumps(contract, ensure_ascii=False))
    except Exception:
        pass

    try:
        for record in state.list_artifacts("pre_registration"):
            artifact = state.read_artifact(record["id"]) or {}
            texts.append("\n".join([
                str(artifact.get("content") or ""),
                json.dumps(artifact.get("metadata") or {}, ensure_ascii=False),
            ]))
    except Exception:
        pass

    text = "\n".join(texts)
    if not text:
        return None
    if re.search(r"\b(?:rocm|hip|oneapi|intel(?:\s+gpu)?|level[ _-]?zero)\b", text, re.I):
        return "non_cuda"
    if re.search(r"\b(?:cuda|nvidia|nvcc|nvfortran|sm_[0-9]+)\b", text, re.I):
        return "nvidia_cuda"
    if re.search(r"\b(?:gpu|openacc|kokkos|sycl)\b", text, re.I):
        return "unknown_gpu"
    return None


def _gpu_skill_injector_on_turn_start(ctx: HookContext) -> list | None:
    state = ctx.state
    if state.hook_state.get("_gpu_skill_injected"):
        return None
    backend = _gpu_backend_hint(state)
    if backend is None:
        return None
    if backend != "nvidia_cuda":
        state.hook_state["_gpu_skill_injected"] = True
        state.append_transcript("gpu_skill_not_applicable", backend=backend, turn=ctx.turn)
        return [_sys(
            "本次实验涉及 GPU，但未确认 NVIDIA CUDA 后端。不要假定 CUDA 参数或构建路线；"
            "先探测实际硬件/编译器，再按目标软件官方文档选择后端。"
        )]
    try:
        from core.skill_registry import get_skill
        if get_skill("gpu-hpc-porting") is None:
            log.warning("GPU run detected but gpu-hpc-porting skill is not registered")
            state.append_transcript("gpu_skill_missing", skill="gpu-hpc-porting", turn=ctx.turn)
            return [_sys("已确认 NVIDIA CUDA 任务，但 gpu-hpc-porting skill 未注册；先阅读目标软件的官方 CUDA 构建文档。")]
        state.hook_state["_gpu_skill_injected"] = True
        state.append_transcript("skill_injected", skill="gpu-hpc-porting", turn=ctx.turn)
        return [_sys(
            "本次 prereg 明确涉及 NVIDIA CUDA。首个 GPU build/run 前调用 "
            "`load_skill(name='gpu-hpc-porting')` 读取 GPU 专属 SOP；不要假定 "
            "ROCm/oneAPI/CUDA 参数可互换。"
        )]
    except Exception as exc:
        log.warning("gpu_skill_injector 失败: %s", exc)
        return None


gpu_skill_injector = LoopHook(
    name="gpu_skill_injector",
    description="仅在 prereg/contract 明确涉及 NVIDIA CUDA 时注入 gpu-hpc-porting skill",
    on_turn_start=_gpu_skill_injector_on_turn_start,
    emits=("skill_injected", "gpu_skill_not_applicable", "gpu_skill_missing"),
)
register_loop_hook(gpu_skill_injector)


# ─────────────────────────────────────────────
# experiment_skill_router —— scope 已确认后才提示 scientific SOP
# ─────────────────────────────────────────────
# classifier 是本节点在首轮工具调用后才写入 state 的，因此路由放 on_turn_end：
# 首轮仍保持内核上下文；下一轮只收到一条可审计的短指针，正文继续由 load_skill 按需取。
#: 049-0：operation category → 该先读的节点 skill。只列有活体证据的：源码构建/安装类
#: 撞墙最多、hpc-build 却最少被读。其余 category 暂不点名（没有证据就不加指针）。
_OPERATION_CATEGORY_SKILLS: dict[str, tuple[str, ...]] = {
    "toolchain_build": ("hpc-build",),
    "package_install": ("hpc-build",),
}
_OPERATION_SKILL_REASON = {
    "hpc-build": "官方构建系统、MPI/ABI/依赖图、构建失败诊断",
}


def _experiment_skill_router_on_turn_end(ctx: HookContext) -> list | None:
    state = ctx.state
    if state.hook_state.get("_scope_skills_routed"):
        return None
    try:
        from tools.run_contract import load_execution_mode_view
        mode_view = load_execution_mode_view(state)
    except Exception:
        return None
    if not mode_view.get("classification_present"):
        return None

    state.hook_state["_scope_skills_routed"] = True
    mode = str(mode_view.get("mode") or "").strip().lower()
    if mode != "scientific":
        # 049-0：operation 也按 category 点名 SOP——源码构建 / 安装类先读 hpc-build。
        # 索引里有它还不够（活体里 hpc-build 只被读 7 次）：分类一落，下一轮就给指针。
        # category 来自收据投影的 run contract（与 ROC 读的同一处），不是可变缓存。
        try:
            from tools.run_contract import load_run_contract
            category = str(load_run_contract(state).get("operation_kind") or "").strip().lower()
        except Exception:
            category = ""
        operation_skills = _OPERATION_CATEGORY_SKILLS.get(category, ())
        if operation_skills:
            state.append_transcript(
                "experiment_skill_routed", scope=mode, category=category,
                skills=list(operation_skills), turn=ctx.turn,
            )
        else:
            state.append_transcript(
                "experiment_skill_routing_not_applicable",
                scope=mode, turn=ctx.turn,
            )
        skill_lines = "".join(
            f"\n- 规划路线前先 `load_skill(name='{name}')`（{_OPERATION_SKILL_REASON[name]}）"
            for name in operation_skills
        )
        return [_sys(
            "scope 已确认为 operation。" + (
                f"本 run 的 category 是 {category}：" + skill_lines + "\n"
                if operation_skills else ""
            ) +
            "只走 operation 收尾：执行/安装 → 最小验证 → "
            "调用 record_operation_completion；该工具是唯一收尾写入器，会生成并按 raw_results "
            "→ clean_results → experiment_log 冻结三件套。不要手工 save/freeze 这三类 artifact，"
            "也不要写科学 verdict。三件套冻结后，只有发现可复用的方法、兼容性结论或真实 dead end 时，才可用冻结 log 作来源创建对应 KB claim；没有价值时不要为沉淀而沉淀，更不要把 operation 伪装成 create_experiment；有外部 job 则在三件套完成后 "
            "finalize_external_job。\n"
            "若 operation 合理受阻（缺必需输入、依赖无法安装、data 无法交付），先调用 "
            "report_blocker，再把返回的 blocker.blocker_id 传给 record_operation_completion(outcome=blocked, blocker_id=...)；"
            "工具会把该结构化 blocker 写入冻结原始 receipt，无需为了收尾手工造文件；仍须给具体 next_step。"
        )]

    try:
        from tools.run_contract import load_run_contract
        contract = load_run_contract(state)
    except Exception:
        contract = {}
    skill_names = ["scientific-results"]
    if (str(contract.get("run_role") or "").lower() == "primary"
            and str(contract.get("stage") or "").lower() == "simulation"):
        skill_names.append("primary-scientific-closure")

    state.append_transcript(
        "experiment_skill_routed", skills=skill_names, turn=ctx.turn,
    )
    lines = [
        "scope 已确认为 scientific。正式收尾必须保留 raw_results 与 clean_results；"
        "随后冻结 experiment_log 并完成适用的科学审计。进行结果处理或正式收尾前，读取适用 SOP："
    ]
    lines.extend(f"- `load_skill(name='{name}')`" for name in skill_names)
    return [_sys("\n".join(lines))]


experiment_skill_router = LoopHook(
    name="experiment_skill_router",
    description="scope 确认后为 scientific run 路由结果与 primary closure SOP",
    on_turn_end=_experiment_skill_router_on_turn_end,
    emits=("experiment_skill_routed", "experiment_skill_routing_not_applicable"),
)
register_loop_hook(experiment_skill_router)


# ─────────────────────────────────────────────
# experiment_scheduler_facts_router —— 外部作业到终态时点名该调度器的终态事实（049-1）
# ─────────────────────────────────────────────
# 终态事实（词表、ExitCode 形状、各终态的「它在什么情况下会说谎」）放在
# scheduler-longrun 的 references/ 里，不常驻。模型需要它的时刻只有一个：
# 外部作业的健康/等待/收尾工具第一次返回 scheduler_phase=terminal。这里按
# (scheduler, job_id) 点名一次，正文仍由 load_skill(asset=…) 按需取。
#: 只列有 references 文件的调度器；local / kubernetes 没有文件就不点名。
_SCHEDULER_FACTS_ASSETS: dict[str, str] = {
    "slurm": "references/slurm-terminal-facts.md",
    "pbs": "references/pbs-torque-terminal-facts.md",
}
_SCHEDULER_FACTS_TOOLS = frozenset({
    "check_external_job_health", "wait_for_external_job", "finalize_external_job",
})
_SCHEDULER_FACTS_ROUTED_KEY = "_scheduler_facts_routed"


def _terminal_scheduler_observations(records: Any) -> list[tuple[str, str]]:
    """本轮工具记录里到达 terminal 的 (scheduler, job_id)，按出现顺序去重。

    check_external_job_health 直接返回 health；wait_for_external_job /
    finalize_external_job 把 health 嵌在 result["health"] 里——两种形状都读。
    """
    seen: list[tuple[str, str]] = []
    for record in records or []:
        if not isinstance(record, dict) or record.get("name") not in _SCHEDULER_FACTS_TOOLS:
            continue
        result = record.get("result")
        if not isinstance(result, dict):
            continue
        health = result.get("health") if isinstance(result.get("health"), dict) else result
        if str(health.get("scheduler_phase") or "").strip().lower() != "terminal":
            continue
        args = record.get("args") if isinstance(record.get("args"), dict) else {}
        scheduler = str(health.get("scheduler") or args.get("scheduler") or "").strip().lower()
        job_id = str(health.get("job_id") or args.get("job_id") or "").strip()
        if scheduler not in _SCHEDULER_FACTS_ASSETS or not job_id:
            continue
        key = (scheduler, job_id)
        if key not in seen:
            seen.append(key)
    return seen


def _experiment_scheduler_facts_router_on_turn_end(ctx: HookContext) -> list | None:
    observed = _terminal_scheduler_observations(getattr(ctx, "tool_call_records", None))
    if not observed:
        return None
    state = ctx.state
    routed = state.hook_state.setdefault(_SCHEDULER_FACTS_ROUTED_KEY, [])
    lines: list[str] = []
    for scheduler, job_id in observed:
        key = f"{scheduler}:{job_id}"
        if key in routed:
            continue
        routed.append(key)
        asset = _SCHEDULER_FACTS_ASSETS[scheduler]
        state.append_transcript(
            "experiment_skill_routed", scope="scheduler_terminal",
            scheduler=scheduler, job_id=job_id,
            skills=["scheduler-longrun"], asset=asset, turn=ctx.turn,
        )
        lines.append(
            f"- {scheduler} 作业 {job_id} 已到终态：解读 State / ExitCode（或 job_state / "
            f"Exit_status）前先 `load_skill(name='scheduler-longrun', asset='{asset}')`。"
        )
    if not lines:
        return None
    return [_sys(
        "外部作业到达终态。终态与退出码的官方事实（含「它在什么情况下会说谎」）在 "
        "scheduler-longrun 的 references 里；判定以工具返回的 terminal_evidence 为准，"
        "退出码低位为 0 不是成功的证据：\n" + "\n".join(lines)
    )]


experiment_scheduler_facts_router = LoopHook(
    name="experiment_scheduler_facts_router",
    description="外部作业到终态后点名该调度器的终态事实 references（049-1）",
    on_turn_end=_experiment_scheduler_facts_router_on_turn_end,
    emits=("experiment_skill_routed",),
)
register_loop_hook(experiment_scheduler_facts_router)


# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# secondary_experiment_log_recovery —— 低资格运行的最小固有记录兜底
# ─────────────────────────────────────────────
# experiment_log 是节点的固有输出物，不能按运行角色拆成另一种 artifact type。
# secondary 运行允许记录很短，但如果 agent 在异常终止前来不及保存，仍需要一份
# 可冻结、明确标注为自动生成且不可用于分析的记录。primary 同样会生成，只是措辞
# 不同 —— 它是运行失败的审计物，不是科研产出：experiment_contract_audit 认
# metadata.auto_generated，对任何 run_role 都不让它满足 verdict / sediment，
# 避免自动记录掩盖运行的不完整性。

async def _secondary_experiment_log_recovery_on_end(
    ctx: HookContext, loop_result: Any,
) -> None:
    state = ctx.state
    if _is_operational_run(state):
        return
    try:
        from tools.run_contract import load_run_contract
        contract = load_run_contract(state)
    except Exception as exc:
        log.warning("secondary experiment_log recovery: 读取 run contract 失败: %s", exc)
        return

    # 只在完全没有 experiment_log 时兜底；已有但未达标的记录必须原样暴露，
    # 由 quality_checks 报告缺项，而不是另写一份“看起来合规”的副本。
    try:
        if current_run_artifacts(state, "experiment_log"):
            return
    except Exception as exc:
        log.warning("secondary experiment_log recovery: 列举 artifact 失败: %s", exc)
        return

    status = (
        loop_result.get("status")
        if isinstance(loop_result, dict)
        else getattr(loop_result, "status", None)
    ) or "unknown"

    # 直接取 create_run_manifest 的返回值，不去猜文件名：artifact 落盘名由
    # name 派生（run_manifest__run_manifest.json），按 run_id 拼路径永远读不到，
    # 会让兜底记录恒缺 returncode/日志 —— 而这正是它唯一的用途。本 hook 排在
    # repro_snapshot 之前，此处生成的是当下快照，repro_snapshot 结束时会用带
    # bundle 信息的终版覆盖同一路径（幂等）。
    returncode: Any = "unknown"
    run_log_paths: list[str] = []
    manifest_available = False
    try:
        from tools.run_contract import create_run_manifest
        manifest = create_run_manifest(state, loop_result=loop_result)
        status = manifest.get("status") or status
        if manifest.get("return_code") is not None:
            returncode = manifest["return_code"]
        run_log_paths = [
            item["path"] for item in (manifest.get("logs") or [])
            if isinstance(item, dict) and item.get("path")
        ]
        manifest_available = True
    except Exception as exc:
        log.warning("secondary experiment_log recovery: run_manifest 生成失败: %s", exc)

    run_role = str(contract.get("run_role") or "secondary")
    if run_role == "primary":
        reason = "primary_run_missing_final_experiment_log"
    else:
        reason = str(
            contract.get("exclusion_reason")
            or "secondary_run_without_final_experiment_log"
        )
    title = "Automatic Primary Failure Record" if run_role == "primary" \
        else "Automatic Secondary Run Record"
    try:
        try:
            from .tools.execution_route import route_correction_witness_disclosure
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.execution_route import route_correction_witness_disclosure
        correction_disclosure = route_correction_witness_disclosure(state)
    except Exception as exc:
        log.warning(
            "secondary experiment_log recovery: 无法派生 route correction "
            "limitation，拒绝生成可误导的终态记录: %s",
            exc,
        )
        return
    correction_check = correction_disclosure.get("check")
    correction_human_projection = (
        json.dumps(
            correction_check,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if correction_check is not None else ""
    )
    content = (
        f"# {title}\n\n"
        "auto_generated: true\n"
        f"run_id: {state.run_id}\n"
        f"run_role: {run_role}\n"
        f"status: {status}\n"
        f"returncode: {returncode}\n"
        f"exclusion_reason: {reason}\n"
        f"log_paths: {json.dumps(run_log_paths, ensure_ascii=False)}\n"
        + ("" if manifest_available else "manifest_unavailable: true\n")
        + "\n"
        "## Verdict\n"
        "verdict: inconclusive\n"
        f"reason: {reason}; no analysis claim is made.\n"
        "next_step: 需要检查 transcript/log 状态并决定补充输入或重跑。\n\n"
        "## Credibility\n"
        "credibility: invalid\n"
        "basis: agent did not produce a final experiment_log; this is an automatic "
        "run-status record only.\n\n"
        "## Methodological / Dead End\n"
        "本次未发现 methodological / dead_end finding，理由：运行未形成可进入分析的"
        "数据证据；需要检查 transcript/log 后重跑。该记录仅用于审计运行角色、状态、"
        "退出码和日志定位，不得作为主实验结果。\n"
        + (
            "\n## Route Correction Witness Limitations\n"
            + correction_human_projection
            + "\n"
            if correction_human_projection else ""
        )
    )
    try:
        saved = state.save_artifact(
            artifact_type="experiment_log",
            name="auto_secondary_run_record",
            content=content,
            metadata={
                "auto_generated": True,
                "incomplete_record": True,
                "terminal_failure_record": True,
                "terminal_status": "inconclusive",
                "recommended_action": "inspect_and_retry_or_redirect",
                "run_id": state.run_id,
                "run_role": run_role,
                "exclusion_reason": reason,
                "route_correction_witness_limitations": (
                    correction_disclosure.get("limitations") or []
                ),
                "route_correction_witness_limitation_check": correction_check,
            },
        )
        from shared.tools.library.artifacts_extra import _freeze_artifact
        frozen = await _freeze_artifact(
            state, saved["id"], reason=f"automatic {run_role} run status record",
        )
        frozen_ok = (frozen or {}).get("status") == "success"

        # This artifact is created at on_end, after the normal citation hook.
        # Emit the same evidence event here so its audit is not skipped.
        citation: dict[str, Any] = {}
        try:
            from shared.lib.citation_integrity import validate_artifact_citations
            citation = validate_artifact_citations(saved["id"], state)
            state.append_transcript(
                "citation_validation",
                turn=getattr(ctx, "turn", 0),
                artifact_id=saved["id"],
                artifact_type="experiment_log",
                passed=citation.get("passed", False),
                n_cited=citation.get("n_cited", 0),
                n_phantom=citation.get("n_phantom", 0),
                phantom_ids=citation.get("phantom_ids", []),
                phantom_with_counts=citation.get("phantom_with_counts", []),
                source="terminal_experiment_log_recovery",
            )
        except Exception as exc:
            citation = {"passed": False, "error": f"citation_audit_error: {exc}"}
            log.warning("secondary experiment_log recovery: citation audit failed: %s", exc)

        terminal_record = {
            "artifact_id": saved["id"],
            "terminal_status": "inconclusive",
            "run_role": run_role,
            "frozen": frozen_ok,
            "citation_validation_passed": bool(citation.get("passed")),
            "n_cited_claims": int(citation.get("n_cited", 0) or 0),
            "n_phantom_claims": int(citation.get("n_phantom", 0) or 0),
            "returncode": returncode,
            "log_paths": run_log_paths,
            "reason": reason,
            "recommended_action": "inspect_and_retry_or_redirect",
        }
        state.hook_state["terminal_experiment_record"] = terminal_record
        state.append_transcript(
            "experiment_log_recovered",
            artifact_id=saved["id"],
            frozen=frozen_ok,
            run_role=run_role,
        )
        state.append_transcript("terminal_experiment_log_generated", **terminal_record)
    except Exception as exc:
        log.warning("secondary experiment_log recovery: 写入失败: %s", exc)


secondary_experiment_log_recovery = LoopHook(
    name="secondary_experiment_log_recovery",
    description="缺少固有 experiment_log 时生成明确不可分析的最小运行记录",
    on_end=_secondary_experiment_log_recovery_on_end,
    emits=("experiment_log_recovered",),
)
register_loop_hook(secondary_experiment_log_recovery)


# ─────────────────────────────────────────────
# experiment_contract_audit —— 终态契约机械审计
# ─────────────────────────────────────────────
# 必须排在 secondary_experiment_log_recovery 之后：secondary/failed run 可能
# 需要先生成明确不可分析的 experiment_log。turn-end adviser 会在可恢复的失败点
# 注入节点专属指引；on-end hook 再统一审计 verdict 和 methodological/dead_end
# sediment。这里不判科学数值，只核对真实 tool_result 和日志中的最小结构，
# quality_checks 可直接读取 transcript 事件。

def _latest_experiment_log_is_frozen(state: Any) -> bool:
    records = current_run_artifacts(state, "experiment_log")
    if not records:
        return False
    record = state.read_artifact(records[-1]["id"]) or {}
    return bool((record.get("metadata") or {}).get("frozen"))


def sediment_closure_advisor_on_turn_end(ctx: HookContext) -> list[LLMMessage] | None:
    """Inject recovery guidance at the exact failed-claim decision point.

    The shared KB error correctly suggests empirical for a single observation,
    but only experiment knows that empirical does not close its sediment duty.
    This hook supplies that node-local affordance immediately after the error.
    """
    if _is_operational_run(ctx.state):
        return None
    messages: list[LLMMessage] = []
    for record in ctx.tool_call_records or []:
        args = record.get("args") or {}
        result = record.get("result") or {}
        error = str(result.get("error") or "")
        if (record.get("name") == "create_claim"
                and args.get("claim_type") == "methodological"
                and result.get("status") == "error"
                # 判据跟着报错口径走：原来是 KB schema 的 HIGH_TIER 闸
                # （independent_source_count ≥ 2），那道闸已删。现在拒收
                # sediment 候选的是节点自己的证据判据（sediment.py）。
                # 两个口径都认 —— 旧 transcript 回放时也得触发。
                and ("没有任何真实来源" in error
                     or "承诺不是证据" in error
                     or "independent_source_count" in error)):
            frozen = _latest_experiment_log_is_frozen(ctx.state)
            ctx.state.append_transcript(
                "sediment_methodological_rejected_guidance",
                turn=ctx.turn, log_frozen=frozen, error=error[:300])
            recovery = (
                "⛔ sediment 候选零证据被拒（预留的 log chunk 是承诺，不是证据）。"
                "empirical 可合法保存单次观察，但不满足 experiment 的 sediment "
                "closure；不要为过门虚构 dead_end。\n"
                "- 正路：先把支撑这条结论的东西变成真实 chunk —— "
                "`freeze_artifact` 冻结实验日志后返回里带 chunk_id，直接用它"
                "（没有单独的登记工具）；或引用文献锚点；\n"
                "- 若存在真实不可复用的失败经验，才可改为 dead_end；\n"
                "- 否则调用 declare_no_sediment(reason=...)。"
            )
            if frozen:
                recovery += ("当前 experiment_log 已冻结；该工具会写绑定该日志的可审计 "
                             "transcript addendum，并在最终状态中显示。")
            else:
                recovery += ("当前 experiment_log 尚未冻结；该工具会把声明同时写入日志。")
            messages.append(_sys(recovery))
            break

    if _latest_experiment_log_is_frozen(ctx.state):
        audit = audit_experiment_contract(ctx.state)
        checks = terminal_closure_projection(audit)["audit_checks"]
        failed = {key: value.get("reason", "未通过")
                  for key, value in checks.items() if not value.get("passed")}
        if failed:
            signature = json.dumps(failed, ensure_ascii=False, sort_keys=True)
            if ctx.state.hook_state.get("_last_experiment_contract_preview") != signature:
                ctx.state.hook_state["_last_experiment_contract_preview"] = signature
                ctx.state.append_transcript(
                    "experiment_contract_preview", turn=ctx.turn,
                    overall_status="incomplete", failed_checks=list(failed), reasons=failed)
                lines = ["⛔ experiment 收尾契约当前未闭环；不得声称完成。"]
                lines.extend(f"- {key}: {reason}" for key, reason in failed.items())
                if "verdict" in failed:
                    lines.append("verdict 恢复：调用 resolve_prereg_hypotheses 定位交接对象；在 experiment_log 记录 measured metrics、阈值比较和 replay evidence。可用测量写 provisional 并交给 Analysis，判不动时调用 declare_inconclusive_verdict(reason=..., next_step=...)；不得翻 hypothesis status。")
                lines.append(
                    "修复后调用 preview_experiment_contract；以返回的完整 "
                    "checks 映射为准，所有已注册终态检查通过且无 "
                    "open_external_jobs，才能称科学实验完成。"
                )
                messages.append(_sys("\n".join(lines)))
    return messages or None


sediment_closure_advisor = LoopHook(
    name="sediment_closure_advisor",
    description="在 sediment claim 被拒或冻结日志未闭环时注入 experiment 专属恢复指引",
    on_turn_end=sediment_closure_advisor_on_turn_end,
    emits=("sediment_methodological_rejected_guidance", "experiment_contract_preview"),
)
register_loop_hook(sediment_closure_advisor)


_TERMINAL_CLOSURE_SNAPSHOT_EVENT = "experiment_terminal_closure_snapshot"
_TERMINAL_CLOSURE_PERSISTENCE_EVENT = (
    "experiment_terminal_closure_persistence_audit"
)


def _terminal_closure_snapshot(
    projection: dict[str, Any],
    *,
    source: str,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """Freeze one complete terminal-closure view before emitting projections.

    Per-gate transcript events are compatibility projections.  Their shared
    ``terminal_closure_snapshot_id`` points back to this one authoritative
    payload, so a later transcript write failure cannot manufacture a second,
    contradictory audit result.
    """
    body: dict[str, Any] = {
        "snapshot_schema_version": 1,
        "source": source,
        "terminal_closure_registry": dict(
            _contract_audit.TERMINAL_CLOSURE_REGISTRY
        ),
        "audit_checks": projection["audit_checks"],
        "failed_audit_keys": list(projection["failed_audit_keys"]),
        "failed_event_keys": list(projection["failed_event_keys"]),
    }
    if failure_reason is not None:
        body["failure_reason"] = failure_reason

    # Normalize exactly the payload that will be persisted.  This prevents a
    # datetime/path-like value accepted by append_transcript(default=str) from
    # producing a digest for a representation different from the durable one.
    normalized = json.loads(json.dumps(
        body,
        ensure_ascii=False,
        default=str,
    ))
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return {
        "passed": not normalized["failed_audit_keys"],
        "snapshot_id": f"sha256:{digest}",
        "snapshot_sha256": digest,
        **normalized,
    }


def _append_terminal_closure_snapshot(
    state: Any,
    snapshot: dict[str, Any],
    persisted_events: list[str],
) -> None:
    """Persist the authority first, then its legacy per-gate projections."""
    state.append_transcript(_TERMINAL_CLOSURE_SNAPSHOT_EVENT, **snapshot)
    persisted_events.append(_TERMINAL_CLOSURE_SNAPSHOT_EVENT)
    registry = snapshot["terminal_closure_registry"]
    checks = snapshot["audit_checks"]
    for audit_key, event_name in registry.items():
        event_payload = dict(checks[audit_key])
        event_payload["terminal_closure_snapshot_id"] = snapshot["snapshot_id"]
        state.append_transcript(event_name, **event_payload)
        persisted_events.append(event_name)


def _record_terminal_closure_persistence_error(
    state: Any,
    loop_result: Any,
    *,
    snapshot: dict[str, Any],
    persisted_events: list[str],
    stage: str,
    exc: Exception,
) -> None:
    """Block incomplete persistence without rewriting computed gate results."""
    detail = f"persistence_error: {type(exc).__name__}: {exc}"
    failed_checks = [
        *snapshot.get("failed_event_keys", []),
        _TERMINAL_CLOSURE_PERSISTENCE_EVENT,
    ]
    if not state.hook_state.get("experiment_downstream_blocked"):
        _block_experiment_completion(
            state,
            loop_result,
            blocker_id="experiment_closure_persistence_error",
            reason="experiment_closure_persistence_error",
            failed_checks=failed_checks,
            summary=(
                "the terminal closure result was computed, but its durable "
                "event projection could not be fully persisted"
            ),
        )
    else:
        # A real failed gate may already be the primary blocker.  Preserve it
        # instead of replacing its authority with an observability failure.
        blockers = state.hook_state.setdefault("blockers", [])
        if not any(
            item.get("blocker_id") == "experiment_closure_persistence_error"
            for item in blockers
            if isinstance(item, dict)
        ):
            blockers.append({
                "blocker_id": "experiment_closure_persistence_error",
                "category": "closure",
                "summary": (
                    "the terminal closure result was computed, but its durable "
                    "event projection could not be fully persisted"
                ),
                "retryable_after_change": True,
            })
        if str(getattr(loop_result, "status", "") or "") not in {
            "cancelled", "paused",
        }:
            try:
                loop_result.status = "blocked"
            except Exception:
                pass
    try:
        state.append_transcript(
            _TERMINAL_CLOSURE_PERSISTENCE_EVENT,
            passed=False,
            applicable=True,
            status="persistence_error",
            reason=detail,
            failed_stage=stage,
            terminal_closure_snapshot_id=snapshot["snapshot_id"],
            snapshot_persisted=(
                _TERMINAL_CLOSURE_SNAPSHOT_EVENT in persisted_events
            ),
            persisted_events=list(persisted_events),
        )
    except Exception:
        log.warning(
            "unable to record terminal closure persistence failure",
            exc_info=True,
        )
    _append_mechanical_failure_footer(
        loop_result,
        {_TERMINAL_CLOSURE_PERSISTENCE_EVENT: detail},
    )



# P0a v3 M3：terminal key 只在 TERMINAL_CLOSURE_REGISTRY 维护一份；operation lane 这几处曾手写字面量。
_PREREG_ASSIGNMENT_AUDIT_EVENT = _contract_audit.TERMINAL_CLOSURE_REGISTRY["prereg_assignment"]

def experiment_contract_audit_on_end(ctx: HookContext, loop_result: Any) -> None:
    state = ctx.state
    if _is_operational_run(state):
        try:
            assignment_audit = audit_prereg_assignment(state)
        except Exception as exc:
            assignment_audit = {
                "passed": False,
                "applicable": True,
                "status": "audit_error",
                "reason": f"audit_error: {type(exc).__name__}: {exc}",
            }
        try:
            state.append_transcript(
                _PREREG_ASSIGNMENT_AUDIT_EVENT, **assignment_audit,
            )
        except Exception as exc:
            log.warning(
                "unable to record experiment_prereg_assignment_audit",
                exc_info=True,
            )
            detail = f"persistence_error: {type(exc).__name__}: {exc}"
            _block_experiment_completion(
                state,
                loop_result,
                blocker_id=(
                    "experiment_prereg_assignment_audit_persistence_error"
                ),
                reason=(
                    "experiment_prereg_assignment_audit_persistence_error"
                ),
                failed_checks=[
                    _PREREG_ASSIGNMENT_AUDIT_EVENT,
                    _TERMINAL_CLOSURE_PERSISTENCE_EVENT,
                ],
                summary=(
                    "the operation assignment audit was computed but could not "
                    "be persisted; completion remains blocked"
                ),
            )
            try:
                state.append_transcript(
                    _TERMINAL_CLOSURE_PERSISTENCE_EVENT,
                    passed=False,
                    applicable=True,
                    status="persistence_error",
                    reason=detail,
                    failed_stage=_PREREG_ASSIGNMENT_AUDIT_EVENT,
                )
            except Exception:
                log.warning(
                    "unable to record operation closure persistence failure",
                    exc_info=True,
                )
            _append_mechanical_failure_footer(
                loop_result,
                {_TERMINAL_CLOSURE_PERSISTENCE_EVENT: detail},
            )
        if not assignment_audit.get("passed", False):
            _block_experiment_completion(
                state,
                loop_result,
                blocker_id="experiment_prereg_assignment_pending",
                reason="experiment_prereg_assignment_pending",
                failed_checks=[_PREREG_ASSIGNMENT_AUDIT_EVENT],
                summary=(
                    "the operation evidence may be preserved, but its prereg "
                    "assignment is still pending upstream dispatch"
                ),
            )
            _append_mechanical_failure_footer(
                loop_result,
                {
                    _PREREG_ASSIGNMENT_AUDIT_EVENT: str(
                        assignment_audit.get("reason")
                        or "prereg assignment audit failed"
                    )
                },
            )
        try:
            result = _audit_operation_log(state)
            state.append_transcript("experiment_operation_audit", **result)
            intent_binding = result.get("intent_binding")
            if isinstance(intent_binding, dict):
                state.append_transcript(
                    "experiment_execution_intent_audit", **intent_binding)
            if result.get("passed"):
                child_projection = operation_child_obligation_projection(state)
                child_obligation = child_projection.get("child_obligation")
                if isinstance(child_obligation, dict):
                    _append_operation_child_delivery_summary(
                        loop_result,
                        child_obligation,
                    )
            outcome_receipt = (
                _operation_outcome_receipt(state, result)
                if result.get("passed")
                else None
            )
            if (outcome_receipt is not None
                    and outcome_receipt["effective_outcome"] != "success"):
                effective = str(outcome_receipt["effective_outcome"])
                requested = str(outcome_receipt["requested_outcome"])
                checks = [f"effective_operation_outcome={effective}"]
                if requested != effective:
                    checks.append(f"requested_operation_outcome={requested}")
                _block_experiment_completion(
                    state,
                    loop_result,
                    blocker_id="experiment_operation_nonsuccess_outcome",
                    reason="experiment_operation_nonsuccess_outcome",
                    failed_checks=checks,
                    summary=(
                        "operation evidence closed honestly with a non-success "
                        f"outcome ({effective}); it must not be summarized as completed"
                    ),
                )
            if not result["passed"]:
                errors = result.get("errors") or []
                detail = str(result.get("reason") or (
                    "; ".join(str(item) for item in errors)
                    if isinstance(errors, list) else errors
                ) or "operation evidence audit failed")
                _block_experiment_completion(
                    state,
                    loop_result,
                    blocker_id="experiment_operation_closure_incomplete",
                    reason="experiment_operation_closure_incomplete",
                    failed_checks=["experiment_operation_audit"],
                    summary="operation evidence is not frozen and closed; downstream review or handoff is blocked",
                )
                _append_mechanical_failure_footer(
                    loop_result,
                    {"experiment_operation_audit": detail},
                )
        except Exception as exc:
            detail = f"audit_error: {type(exc).__name__}: {exc}"
            log.warning("operation log audit failed: %s", exc)
            try:
                state.append_transcript(
                    "experiment_operation_audit",
                    passed=False,
                    reason=detail,
                )
            except Exception:
                log.warning("unable to record operation audit failure", exc_info=True)
            _block_experiment_completion(
                state,
                loop_result,
                blocker_id="experiment_operation_audit_error",
                reason="experiment_operation_audit_error",
                failed_checks=["experiment_operation_audit"],
                summary="operation evidence audit raised an exception; downstream review or handoff is blocked",
            )
            _append_mechanical_failure_footer(
                loop_result,
                {"experiment_operation_audit": detail},
            )
        return
    snapshot: dict[str, Any] | None = None
    persisted_events: list[str] = []
    persistence_stage = "audit_computation"
    try:
        result = audit_experiment_contract(state)
        projection = terminal_closure_projection(result)
        snapshot = _terminal_closure_snapshot(projection, source="audit")
        persistence_stage = "terminal_closure_snapshot"
        _append_terminal_closure_snapshot(
            state, snapshot, persisted_events,
        )
        persistence_stage = "experiment_kb_registration_audit"
        state.append_transcript(
            "experiment_kb_registration_audit", **result["execution_record"])
        persisted_events.append("experiment_kb_registration_audit")
        persistence_stage = "terminal_experiment_record_audit"
        state.append_transcript(
            "terminal_experiment_record_audit", **result["terminal_failure_record"])
        persisted_events.append("terminal_experiment_record_audit")
        persistence_stage = "terminal_closure_projection"
        intent_binding = projection["audit_checks"]["execution_intent_binding"]
        terminal = result["terminal_failure_record"]
        if terminal.get("applicable") and terminal.get("passed"):
            current = str(getattr(loop_result, "final_text", "") or "")
            footer = (
                "\n\n## Terminal Failure Record\n"
                "- terminal_status: inconclusive\n"
                f"- artifact_id: {terminal.get('artifact_id', '')}\n"
                f"- recommended_action: {terminal.get('recommended_action', 'inspect_and_retry_or_redirect')}\n"
                "- note: this is frozen operational evidence, not a scientific result.\n"
            )
            if "## Terminal Failure Record" not in current:
                try:
                    loop_result.final_text = current + footer
                except Exception:
                    log.warning("无法将 terminal failure record 写入 final_text")

        # These are evidence-existence/integrity gates only. Credibility, citation
        # quality, and other reviewer-facing QC findings stay in quality checks and
        # must not be added here as node-terminal verdicts.
        # The model narrative is not authoritative: a failed evidence gate must
        # override a prose claim of completion.
        closure_checks = projection["event_checks"]
        failed_checks = {name: check for name, check in closure_checks.items()
                         if not check.get("passed", False)}
        if failed_checks:
            legacy_authority_failure = (
                "experiment_execution_intent_audit" in failed_checks
                and intent_binding.get("status")
                == "run_authority_receipt_missing_for_existing_scope"
            )
            if legacy_authority_failure:
                ordered_failed_checks = [
                    "experiment_execution_intent_audit",
                    *sorted(
                        name for name in failed_checks
                        if name != "experiment_execution_intent_audit"
                    ),
                ]
                consequential_failed_checks = ordered_failed_checks[1:]
                causal_note = (
                    "the remaining failed checks are fail-closed consequences "
                    "of this legacy run's missing acceptance authority"
                )
            else:
                # Keep the pre-existing model-facing order and machine-facing
                # sorted order byte-for-byte for ordinary accepted/conflicted runs.
                ordered_failed_checks = list(failed_checks)
                consequential_failed_checks = []
                causal_note = ""
            current = str(getattr(loop_result, "final_text", "") or "")
            lines = ["", "", "## Framework Mechanical Status",
                     "- overall_status: incomplete"]
            for index, name in enumerate(ordered_failed_checks):
                check = failed_checks[name]
                lines.append(f"- {name}: failed")
                lines.append(f"  reason: {check.get('reason', 'mechanical audit failed')}")
                if legacy_authority_failure and index == 0:
                    lines.append(f"  note: {causal_note}.")
            sediment = result["sediment"]
            verdict = result["verdict"]
            if sediment.get("late_declaration"):
                lines.append("- note: sediment explicit-none was recorded after log freeze; see transcript addendum.")
            if verdict.get("late_declaration"):
                lines.append("- note: verdict inconclusive declaration was recorded after log freeze; see transcript addendum.")
            lines.append("- note: the model narrative is not an authoritative completion status.")
            footer = "\n".join(lines)
            blocked_state = {
                "reason": "experiment_closure_incomplete",
                "failed_checks": (
                    ordered_failed_checks
                    if legacy_authority_failure
                    else sorted(failed_checks)
                ),
                "review_eligibility": False,
            }
            blocked_event = {
                "failed_checks": list(blocked_state["failed_checks"]),
                "review_eligibility": False,
            }
            if legacy_authority_failure:
                blocked_state.update({
                    "primary_failed_check": "experiment_execution_intent_audit",
                    "consequential_failed_checks": consequential_failed_checks,
                    "causal_note": causal_note,
                })
                blocked_event.update({
                    "primary_failed_check": "experiment_execution_intent_audit",
                    "consequential_failed_checks": consequential_failed_checks,
                    "causal_note": causal_note,
                })
            state.hook_state["experiment_downstream_blocked"] = blocked_state
            blockers = state.hook_state.setdefault("blockers", [])
            if not any(item.get("blocker_id") == "experiment_closure_incomplete" for item in blockers if isinstance(item, dict)):
                blockers.append({"blocker_id": "experiment_closure_incomplete", "category": "closure", "summary": "experiment evidence is not frozen and closed; downstream review or hypothesis handoff is blocked", "retryable_after_change": True})
            state.append_transcript("experiment_downstream_blocked", **blocked_event)
            try:
                loop_result.status = "blocked"
            except Exception:
                pass
            if "## Framework Mechanical Status" not in current:
                try:
                    loop_result.final_text = current + footer
                except Exception:
                    log.warning("无法将机械审计状态写入 final_text")
        elif result["sediment"].get("late_declaration") or result["verdict"].get("late_declaration"):
            current = str(getattr(loop_result, "final_text", "") or "")
            lines = ["", "", "## Framework Closure Addendum"]
            if result["verdict"].get("late_declaration"):
                lines.append("- verdict: an explicit inconclusive declaration was recorded after experiment_log freeze; see its transcript addendum.")
            if result["sediment"].get("late_declaration"):
                lines.append("- sediment: explicit-none was recorded after experiment_log freeze; see its transcript addendum.")
            footer = "\n".join(lines) + "\n"
            if "## Framework Closure Addendum" not in current:
                try:
                    loop_result.final_text = current + footer
                except Exception:
                    log.warning("无法将 closure addendum 写入 final_text")
    except Exception as exc:
        if snapshot is not None:
            # The terminal audit already has one immutable meaning.  A later
            # transcript/projection failure must not emit a second all-failed
            # interpretation of the same audit.
            log.warning(
                "experiment terminal closure persistence failed at %s: %s",
                persistence_stage,
                exc,
            )
            _record_terminal_closure_persistence_error(
                state,
                loop_result,
                snapshot=snapshot,
                persisted_events=persisted_events,
                stage=persistence_stage,
                exc=exc,
            )
            return
        # The durable blocker is written before any transcript event: on_end
        # swallows secondary hook failures, so transcript I/O cannot reopen a
        # failed scientific closure audit.
        log.warning("experiment_contract_audit 失败：%s", exc)
        detail = f"audit_error: {type(exc).__name__}: {exc}"
        fallback = terminal_closure_projection({}, failure_reason=detail)
        fallback_snapshot = _terminal_closure_snapshot(
            fallback,
            source="audit_error",
            failure_reason=detail,
        )
        _block_experiment_completion(
            state,
            loop_result,
            blocker_id="experiment_closure_audit_error",
            reason="experiment_closure_audit_error",
            failed_checks=fallback["failed_event_keys"],
            summary="experiment closure audit raised an exception; downstream review or hypothesis handoff is blocked",
        )
        fallback_persisted_events: list[str] = []
        try:
            _append_terminal_closure_snapshot(
                state, fallback_snapshot, fallback_persisted_events,
            )
        except Exception as persistence_exc:
            log.warning(
                "unable to persist terminal closure audit-error snapshot",
                exc_info=True,
            )
            _record_terminal_closure_persistence_error(
                state,
                loop_result,
                snapshot=fallback_snapshot,
                persisted_events=fallback_persisted_events,
                stage="audit_error_snapshot",
                exc=persistence_exc,
            )
        # Auxiliary observability remains separate from terminal closure.
        try:
            state.append_transcript(
            "experiment_kb_registration_audit",
            passed=False,
            required=False,
            registration_status="audit_error",
            n_registrations=0,
            n_linked_to_frozen_log=0,
            auto_generated_record=False,
            reason=f"audit_error: {type(exc).__name__}: {exc}",
        )
        except Exception:
            log.warning("unable to record experiment_kb_registration_audit failure", exc_info=True)
        _append_mechanical_failure_footer(
            loop_result,
            {name: detail for name in fallback["failed_event_keys"]},
        )

experiment_contract_audit = LoopHook(
    name="experiment_contract_audit",
    description="机械审计 verdict、methodological/dead_end sediment 与执行证据闭环与可选 KB 登记状态",
    on_end=experiment_contract_audit_on_end,
    emits=(*_contract_audit.TERMINAL_CLOSURE_REGISTRY.values(),
           _TERMINAL_CLOSURE_SNAPSHOT_EVENT,
           _TERMINAL_CLOSURE_PERSISTENCE_EVENT,
           "experiment_kb_registration_audit", "terminal_experiment_record_audit",
           "experiment_operation_audit"),
)
register_loop_hook(experiment_contract_audit)


# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# repro_snapshot —— 实验结束自动采集可复现环境快照（需求 ⑦.3）
# ─────────────────────────────────────────────
# 设计动机：科研可复现要求"换台机器/过几个月照着重来"。但工具版本/git commit/
# LD_LIBRARY_PATH/编译 flags 平时只散在 transcript 的 run_bash 命令里，没结构化。
# on_end 自动采集成 environment_snapshot artifact（不靠 agent 自觉——06-10 教训）。

_REPRO_LIBS = ["libpnetcdf.so", "libnetcdf.so", "libnetcdff.so", "libhdf5.so", "libesmf"]


def _repro_snapshot_on_turn_start(ctx: HookContext) -> None:
    """Create a cheap initial record before expensive work starts.

    There is no run-level on_start hook in the current core.  The first turn
    is the earliest node-local point available; an untouched ``running``
    manifest later distinguishes an abrupt termination from a clean finish.
    """
    state = ctx.state
    if state.hook_state.get("_run_manifest_started"):
        return
    try:
        from tools.run_contract import create_run_manifest
        create_run_manifest(state, status="running")
        state.hook_state["_run_manifest_started"] = True
    except Exception as e:
        log.warning("run_manifest 起始记录失败: %s", e)


def _repro_snapshot_on_end(ctx: HookContext, loop_result: Any) -> None:
    state = ctx.state
    # 1. 读取 transcript，交给 repro_snapshot 的通用路径识别器推断真实源码 repo。
    transcript_text = ""
    try:
        tp = getattr(state, "transcript_path", None)
        if tp and Path(tp).exists():
            transcript_text = Path(tp).read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        log.debug("repro_snapshot: 读取 transcript 失败: %s", e)

    # 2. 采集快照
    try:
        from tools.repro_snapshot import (
            collect_snapshot, render_markdown, discover_repo_paths,
            select_experiment_log_result, create_repro_bundle,
        )
        from tools.run_contract import create_run_manifest, load_run_contract
    except Exception as e:
        log.warning("repro_snapshot 导入失败: %s", e)
        return

    # 3. P2/⑦.6：从 experiment_log 提取结构化 verdict + credibility
    result_info: dict = {}
    exp_log_path = None
    try:
        exp_log_path, result_info = select_experiment_log_result(state)
    except Exception as e:
        log.debug("repro_snapshot: 提取 verdict/credibility 失败: %s", e)

    contract = load_run_contract(state)
    owes_verdict = bool(contract.get("requires_hypothesis_verdict"))
    bench_disabled = _bench_off("repro_snapshot")

    # Benchmark A 仍然需要轻量运行记录；开关只跳过昂贵的环境快照和 bundle。
    snap: dict[str, Any] = {}
    md = ""
    snapshot_error = None
    if not bench_disabled:
        try:
            repo_paths = discover_repo_paths(state, transcript_text=transcript_text)
            snap = collect_snapshot(repo_paths=repo_paths, extra_libs=_REPRO_LIBS)
            md = render_markdown(snap)
        except Exception as e:
            snapshot_error = f"{type(e).__name__}: {e}"
            log.warning("repro_snapshot 采集失败: %s", e)

    # 4. 存成 environment_snapshot artifact（实验复现卡片：环境 + 可信度 + verdict）
    if snap:
        try:
            state.save_artifact(
                artifact_type="environment_snapshot",
                name=f"env_snapshot_{state.run_id}",
                content=md,
                metadata={
                    "run_id": state.run_id,
                    "project_id": getattr(state, "project_id", None),
                    "node_type": getattr(state, "node_type", "experiment"),
                    "auto_collected": True,
                    "structured": snap,
                    "tool_count": len(snap.get("toolchain", {})),
                    "repos": list(snap.get("source_versions", {}).keys()),
                    **result_info,   # verdict / verdict_claim_id / credibility
                },
            )
        except Exception as e:
            log.warning("repro_snapshot 存档失败: %s", e)

    # 5. 只有欠正式裁决的运行生成 bundle；secondary 仅保留 experiment_log +
    #    run_manifest，不复制原始数据。判据从 analysis_eligible 换成
    #    requires_hypothesis_verdict：有结论要交的运行就得可复现，哪怕执行前提
    #    有见证在案（owner 2026-09-11，代价是这类运行多打一次包）。
    repro_bundle = None
    if not bench_disabled and owes_verdict:
        try:
            repro_bundle = create_repro_bundle(
                state, snap, result_info, experiment_log_path=exp_log_path,
                transcript_text=transcript_text, force=owes_verdict)
        except Exception as e:
            log.warning("repro_bundle 生成失败: %s", e)

    bundle_info = {
        "created": bool(repro_bundle),
        "reason": (
            "owes_hypothesis_verdict" if owes_verdict else
            "benchmark_disabled" if bench_disabled else
            "snapshot_failed" if snapshot_error else "secondary_run"
        ),
    }
    if repro_bundle:
        bundle_info.update({
            "path": repro_bundle.get("bundle_path"),
            "manifest_sha256": repro_bundle.get("manifest_sha256"),
        })
    if snapshot_error:
        bundle_info["snapshot_error"] = snapshot_error

    # 最终轻量记录：它只索引已存在的 logs/transcript，不复制原始内容。
    try:
        create_run_manifest(
            state, result_info=result_info, loop_result=loop_result,
            bundle=bundle_info, experiment_log_path=exp_log_path)
    except Exception as e:
        log.warning("run_manifest 结束记录失败: %s", e)

    try:
        state.append_transcript(
            "repro_snapshot_saved", run_id=state.run_id,
            tool_count=len(snap.get("toolchain", {})),
            repos=list(snap.get("source_versions", {}).keys()),
            credibility=result_info.get("credibility"),
            verdict=result_info.get("verdict"),
            repro_bundle_path=(repro_bundle or {}).get("bundle_path"),
        )
    except Exception:
        pass


repro_snapshot = LoopHook(
    name="repro_snapshot",
    description="记录每次运行 manifest；按运行资格采集环境快照并生成可复现包",
    on_turn_start=_repro_snapshot_on_turn_start,
    on_end=_repro_snapshot_on_end,
)
register_loop_hook(repro_snapshot)


# ─────────────────────────────────────────────
# workflow_frame —— 阶段帧渲染 + 软约束（弱模型脚手架，纯 node-side）
# ─────────────────────────────────────────────
# 设计：把"每轮喂给模型的整本规则书"换成"当前阶段的小帧"。降认知负荷，
# 用约束(收窄动作菜单)而非指令(再加规则)来引导弱模型。
# 不碰 core：只用现有 on_turn_start 注入 + on_turn_end 检测两个扩展点。
# off-by-default：hook_state 里没有 _workflow 时整段 no-op，不影响任何现有 run。
#
# _workflow 状态（存 state.hook_state["_workflow"]，由 recon 阶段或测试 seed 填充）：
#   active, stage_id, stage_idx, stage_total, goal, success,
#   mode ∈ {execute, diagnose, understand},
#   verified[], blocker, tried[], budget_total, budget_used,
#   menu[{action,hint,note,off,off_reason}]  # 按成本排序 cheap-first,
#   knowledge[]

_WF_KEY = "_workflow"

# 源码文件特征：检测"诊断/理解模式下读工具源码"这一反模式（通用，不绑定具体工具）
_SOURCE_EXTS = (".py", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
                ".f", ".f90", ".f77", ".for", ".f03")


def _looks_like_source(path: str, workspace) -> bool:
    """path 是某工具的实现源码（而非实验自己的输入/输出文件）？"""
    if not path:
        return False
    p = path.strip()
    if workspace and p.startswith(str(workspace)):
        return False  # 实验工作区内的文件不算"工具源码"
    return p.lower().endswith(_SOURCE_EXTS)


def _render_workflow_frame(wf: dict) -> str:
    idx, total = wf.get("stage_idx", "?"), wf.get("stage_total", "?")
    lines = [
        f"[workflow] 阶段 {idx}/{total} ── {wf.get('stage_id', '?')}    "
        f"[模式: {wf.get('mode', 'execute')}]",
        f"目标:     {wf.get('goal', '')}",
        f"成功判据: {wf.get('success', '')}",
    ]
    if wf.get("verified"):
        lines.append("已确认可用: " + " / ".join(wf["verified"]))
    if wf.get("blocker"):
        lines.append(f"当前 blocker: {wf['blocker']}")
    tried = wf.get("tried") or []
    if tried:
        lines.append(f"本阶段已试({len(tried)}):")
        lines += [f"  {i}. {t}" for i, t in enumerate(tried, 1)]
    bt, bu = wf.get("budget_total", 0), wf.get("budget_used", 0)
    left = max(0, bt - bu)
    budget = f"诊断预算:  剩 {left}/{bt}"
    if left <= 0:
        budget += "  ⚠️ 已耗尽 —— 本轮只允许 web_search 或 request_human_input"
    elif left == 1:
        budget += "  ⚠️ 用完后必须 web_search 或求助"
    lines.append(budget)
    menu = wf.get("menu") or []
    if menu:
        lines.append("此刻允许的动作（按成本排序，cheap-first）:")
        for m in menu:
            if m.get("off"):
                lines.append(f"  ✗ {m.get('hint', '')}  ── {m.get('off_reason', '此刻不可用')}")
            else:
                note = f"   ← {m['note']}" if m.get("note") else ""
                lines.append(f"  ▶ {m.get('action', '')}  {m.get('hint', '')}{note}")
    if wf.get("knowledge"):
        lines.append("本阶段相关知识（仅这一阶段）:")
        lines += [f"  • {k}" for k in wf["knowledge"]]
    return "\n".join(lines)


def workflow_frame_on_turn_start(ctx: HookContext) -> list | None:
    """active 时把当前阶段帧作为本轮工作上下文注入。"""
    wf = ctx.state.hook_state.get(_WF_KEY)
    if not wf or not wf.get("active"):
        return None
    return [_sys(_render_workflow_frame(wf))]


def workflow_frame_on_turn_end(ctx: HookContext) -> list | None:
    """软约束：诊断/理解模式下读了工具源码 → 扣诊断预算 + 注入纠正。"""
    wf = ctx.state.hook_state.get(_WF_KEY)
    if not wf or not wf.get("active"):
        return None
    msgs = []
    if wf.get("mode") in ("diagnose", "understand"):
        workspace = getattr(ctx.state, "root", None)
        for rec in ctx.tool_call_records:
            if rec.get("name") != "read_file":
                continue
            path = (rec.get("args") or {}).get("path", "")
            if _looks_like_source(path, workspace):
                wf["budget_used"] = wf.get("budget_used", 0) + 1
                wf.setdefault("tried", []).append(
                    f"读源码 {path}（{wf['mode']} 模式反模式，已扣预算）")
                msgs.append(_sys(
                    f"[workflow] ⚠️ 你在 {wf['mode']} 模式读了源码 {path}。"
                    "源码是实现细节、最贵的信息源，已扣 1 诊断预算。"
                    "回菜单：先 `<tool> --help`、官方文档、web_search——接口侧信息几乎总更快给出答案。"
                ))
                break  # 一轮只罚一次
        ctx.state.hook_state[_WF_KEY] = wf
    return msgs or None


workflow_frame = LoopHook(
    name="workflow_frame",
    description="阶段帧渲染 + 软约束（弱模型脚手架，off-by-default，不碰 core）",
    on_turn_start=workflow_frame_on_turn_start,
    on_turn_end=workflow_frame_on_turn_end,
)
register_loop_hook(workflow_frame)


# ─────────────────────────────────────────────
# output_corruption_sentinel —— LLM 输出损坏的通用哨兵（任意模型/文种）
# ─────────────────────────────────────────────
# 设计动机（2026-06-12 审计）：deepseek 系在高温采样下高频往工具参数里
# 塞损坏 token：数字字段出现 "careful 80"/"快"/"írásvédelem30"，命令串混入
# "和相关"/"weapon" 等随机多语 token。一个 run 实测 52 次参数型损坏 +
# ~10-15% 轮次浪费。模型自己不知道这是输出损坏，每次当成普通报错重试。
#
# 通用性原则：检测的是"损坏类"而非"中文"——
#   ① 数字参数塞了非数字（任意文种的垃圾都抓）
#   ② 工具返回类型错误（invalid literal / not supported between instances）
#   ③ 失败命令里混着非 ASCII token（成功命令永不检查 → 中文 echo/grep 不误报）
# 只在有实证时注入（零损坏零开销），3 轮冷却防刷屏。无需 seed，真实 run 即生效。

_CORRUPT_LAST_INJECT_KEY = "_corruption_last_inject"
_CORRUPT_COUNT_KEY = "_corruption_total"
_CORRUPT_COOLDOWN = 3

_NUMERIC_PARAMS = ("timeout", "limit", "offset", "max_chars")

_TYPE_ERR_RE = _re.compile(r"invalid literal for int|not supported between instances")
_NON_ASCII_TOKEN_RE = _re.compile(r"\S*[^\x00-\x7F]+\S*")


def _find_corruptions(records: list) -> list[str]:
    """从本轮工具调用记录里提取损坏证据（确定性，不靠模型自评）。"""
    found: list[str] = []
    for rec in records:
        args = rec.get("args") or {}
        name = rec.get("name", "?")
        result = rec.get("result")
        # ① 数字参数塞了非数字
        for p in _NUMERIC_PARAMS:
            v = args.get(p)
            if isinstance(v, str) and v.strip() and not v.strip().lstrip("-").isdigit():
                found.append(f"{name}.{p}={v!r}（应为纯数字）")
        # ② 类型错误结果（参数损坏的下游症状）
        if isinstance(result, dict) and result.get("status") == "error":
            err = str(result.get("error", ""))
            if _TYPE_ERR_RE.search(err):
                found.append(f"{name} 参数类型损坏 → {err[:60]}")
        # ③ 失败的 bash 命令里混着非 ASCII token（成功命令不检查，避免误报合法中文）
        if name in _BASH_TOOL_NAMES and isinstance(result, dict):
            failed = result.get("returncode", 0) != 0 or result.get("status") in ("error", "timeout")
            if failed:
                cmd = result.get("cmd", "") or args.get("cmd", "")
                # 先剥掉引号段（echo "中文"、grep '关键词' 是合法用法），再找裸露的非 ASCII token
                unquoted = _re.sub(r'"[^"]*"|\'[^\']*\'', " ", cmd)
                # 注释行里的中文也是合法的
                unquoted = "\n".join(ln for ln in unquoted.splitlines()
                                     if not ln.strip().startswith("#"))
                suspicious = _NON_ASCII_TOKEN_RE.findall(unquoted)
                if suspicious:
                    found.append(f"失败命令含异常 token: {' '.join(suspicious[:2])[:50]}")
    return found


def output_corruption_sentinel_on_turn_end(ctx: HookContext) -> list | None:
    found = _find_corruptions(ctx.tool_call_records)
    if not found:
        return None
    hook_state = ctx.state.hook_state
    total = hook_state.get(_CORRUPT_COUNT_KEY, 0) + len(found)
    hook_state[_CORRUPT_COUNT_KEY] = total
    last = hook_state.get(_CORRUPT_LAST_INJECT_KEY, -999)
    if ctx.turn - last < _CORRUPT_COOLDOWN:
        return None
    hook_state[_CORRUPT_LAST_INJECT_KEY] = ctx.turn
    examples = "\n".join(f"  - {f}" for f in found[:3])
    return [_sys(
        f"[corruption_sentinel] ⚠️ 检测到你的工具参数中出现**输出损坏 token**（本 run 累计 {total} 次）：\n"
        f"{examples}\n"
        "这不是环境或命令本身的问题，是你的输出偶发夹带了乱码。处理方式：\n"
        "① 直接**重新发出干净的命令**（数字参数只写数字，命令避免夹带说明性文字）；\n"
        "② 这类损坏也可能以纯 ASCII 形式出现（命令里混入无关单词）——"
        "当一条命令以你无法解释的方式失败时，先逐字检查命令本身再怀疑环境。"
    )]


# ── 复读锁死检测（2026-06-12 实测：temp=0.2 下 58% 轮次逐字复读 hook 注入文本，
# 自我条件化锁死，最终以纯复读+零工具调用静默终止 run）。
# on_llm_response 只观察 → 记连续复读计数；下一轮 on_turn_start 注入打断。
_ECHO_COUNT_KEY = "_echo_consecutive"
_ECHO_LAST_INJECT_KEY = "_echo_last_inject"
_ECHO_REASON_KEY = "_echo_reason"
_ECHO_TAGS = ("[rules_reminder]", "[strategic_review]", "[plan_before_compile]",
              "[repeated_error_detector]", "[workflow]", "[corruption_sentinel]",
              "[failure_detector]", "[toolchain_guard]", "[execution_control]")


def _echo_lock_reason(content: str) -> str | None:
    """Detect assistant replies that mostly echo hook/system scaffolding."""
    if not content:
        return None
    stripped = content.strip()
    for tag in _ECHO_TAGS:
        if stripped.count(tag) >= 2:
            return f"repeated {tag}"
        if stripped.startswith(tag):
            return f"starts_with {tag}"
    if stripped.count("DO NOT QUOTE THIS BLOCK") >= 2:
        return "repeated execution_control instruction"
    if stripped.count("修复优先级") >= 2 and stripped.count("当前阻塞焦点") >= 2:
        return "repeated execution_control ledger"
    return None


def _unresolved_external_workflows(state: Any) -> list[dict[str, Any]]:
    """Thin import-mode wrapper around the shared strict current-run preview."""
    try:
        from tools.resource_manager import current_run_owed_external_workflows
    except ImportError:
        from .tools.resource_manager import current_run_owed_external_workflows
    return current_run_owed_external_workflows(state)


def _running_external_jobs_are_handoff_ready(
    state: Any,
    running: list[dict[str, Any]],
) -> bool:
    """A healthy wait must match a currently running job before handoff."""
    try:
        try:
            from tools.resource_manager import external_job_handoff_ready
        except ImportError:
            from .tools.resource_manager import external_job_handoff_ready
        permit = external_job_handoff_ready(state)
    except Exception:
        return False
    if not permit:
        return False
    scheduler = str(permit.get("scheduler") or "").casefold()
    job_id = str(permit.get("job_id") or "")
    namespace = str(permit.get("namespace") or "")
    launch_host = str(permit.get("launch_host") or "").casefold()
    scheduler_cluster = str(permit.get("scheduler_cluster") or "").casefold()
    resource_uid = str(permit.get("resource_uid") or "")
    submission_nonce = str(permit.get("submission_nonce") or "")
    container_runtime_id = str(permit.get("container_runtime_id") or "")
    if not scheduler or not job_id:
        return False
    for row in running:
        if not (
            str(row.get("scheduler") or "").casefold() == scheduler
            and str(row.get("job_id") or "") == job_id
            and str(row.get("namespace") or "") == namespace
            and str(row.get("launch_host") or "").casefold() == launch_host
            and str(row.get("scheduler_cluster") or "").casefold() == scheduler_cluster
            and str(row.get("resource_uid") or "") == resource_uid
        ):
            continue
        row_nonce = str(row.get("submission_nonce") or "")
        row_runtime_id = str(row.get("container_runtime_id") or "")
        if scheduler == "local":
            # Local job names are reusable.  A legacy permit cannot authorize
            # whichever container later receives the same name.
            if not submission_nonce or not row_nonce:
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", container_runtime_id):
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", row_runtime_id):
                continue
            if container_runtime_id != row_runtime_id or submission_nonce != row_nonce:
                continue
            return True
        # Remote schedulers retain their scoped stable identity contract.  New
        # fields narrow it when available; legacy remote permits stay valid.
        if submission_nonce and submission_nonce != row_nonce:
            continue
        if container_runtime_id and container_runtime_id != row_runtime_id:
            continue
        return True
    return False


def _external_job_finish_gate(ctx: HookContext) -> list[LLMMessage] | None:
    """收尾闸：本 run 还有未收尾的受管外部作业时否决一次收尾。

    一次成功的 ``submit_job`` 只是一个执行步骤；作业没有走到 finalize 或正式交接
    之前，run 不该结束。本闸跑在框架的 ``on_before_finish`` 相位：返回非空消息即
    否决本次收尾，模型多得一轮。

    历史：这段逻辑原先跑在 ``on_llm_response``，靠改写 LLM response 直接塞进一个
    工具调用。框架在 v3.1 把该相位改成只读（hook 收到深拷贝），experiment 的迁移
    豁免于 2026-08-01 到期，此后闸只写事件、不产生任何效果（2026-09-08 活体：模型
    无工具调用停机 → 闸记录 insert_closure_reminder → 同一秒 run 结束）。改走正门后
    不再依赖任何豁免。

    与旧实现的三点差异，都写进注入文本，不对模型隐瞒：
      * 框架的闸每个 run 只放行一次拦截（``_finish_gate_used``），之后靠 on_end 的
        持久 blocker 兜底；这正是 ROADMAP N-005 当初声明的"一次有界恢复机会"。
      * 注入的是消息不是伪造的工具调用，模型自己决定调什么。
      * 模型空回复停机时框架走空轮事务回滚+有界重试，不经过本相位。

    文本按实际观测状态生成，不按 run 模式分叉：哪个作业、现在什么状态、此刻该调
    哪个工具。``outcome`` 该用哪套词表由作业自己的 execution_class 决定，判断权归
    ``finalize_external_job``，本闸不复制那套判断。
    """
    try:
        workflows = _unresolved_external_workflows(ctx.state)
    except SubmissionLedgerError as exc:
        reason = submission_ledger_failure_reason(exc)
        try:
            ctx.state.append_transcript(
                "external_job_closure_gate",
                turn=ctx.turn,
                action="block_submission_ledger_unreadable",
                failed_checks=[SUBMISSION_LEDGER_FAILED_CHECK],
                reason=reason,
            )
        except Exception:
            pass
        return [_sys(
            "⛔ [external_job_closure_gate] " + reason + "\n"
            "提交结果或身份仍未决；不要重复提交。先按原 submission intent "
            "恢复或核对调度器事实，使该收据形成可验证身份/结果，再重新预览。"
        )]
    if not workflows:
        return None
    running = [row for row in workflows
               if row.get("workflow_status") == "awaiting_external_job"]
    if (len(running) == len(workflows)
            and _running_external_jobs_are_handoff_ready(ctx.state, running)):
        # 交接许可已经匹配到在跑的作业，``external_job_handoff_on_end`` 会持久化
        # 开放 workflow 与 blocker。不再拦一次同样的等待。
        try:
            ctx.state.append_transcript(
                "external_job_closure_gate", turn=ctx.turn,
                action="allow_persistent_handoff", workflows=workflows,
            )
        except Exception:
            pass
        return None

    lines = [f"⛔ [external_job_closure_gate] 本 run 还有 {len(workflows)} 个受管外部作业"
             "没有收尾，现在不能结束。"]
    for row in workflows:
        scheduler = str(row.get("scheduler") or "")
        job_id = str(row.get("job_id") or "")
        health = row.get("health") if isinstance(row.get("health"), dict) else {}
        observed = ", ".join(
            f"{key}={health.get(key)}"
            for key in ("scheduler_phase", "health_state")
            if health.get(key) is not None
        ) or f"workflow_status={row.get('workflow_status')}"
        lines.append(f"- {scheduler}:{job_id}　观测：{observed}")
        if row.get("workflow_status") == "awaiting_external_job":
            args = {"scheduler": scheduler or "local", "job_id": job_id}
            if row.get("namespace"):
                args["namespace"] = str(row["namespace"])
            try:
                args["max_wait_s"] = min(
                    900, max(30, int(health.get("expected_duration_s") or 300)))
            except (TypeError, ValueError):
                args["max_wait_s"] = 300
            lines.append("  下一步：wait_for_external_job("
                         + ", ".join(f"{k}={v!r}" for k, v in args.items()) + ")")
        else:
            args = {"scheduler": scheduler or "local", "job_id": job_id}
            if row.get("namespace"):
                args["namespace"] = str(row["namespace"])
            lines.append("  下一步：先看输出，再 finalize_external_job("
                         + ", ".join(f"{k}={v!r}" for k, v in args.items())
                         + ", outcome=…)")
    lines.append(
        "outcome 按该作业自己的 execution_class 选：simulation 类用 analyzed_* 一族，"
        "diagnostic 与 toolchain_build 类用 operation_* 一族；execution_class 记在该作业的 "
        "job_submission 产物里，finalize_external_job 的 schema 列出了合法取值。")
    lines.append(
        "finalize 若拒绝，拒绝信息里带的就是权威状态与合法出口，按它走；不要改用 "
        "cancel_job 绕开——把读得出终态的作业记成 cancelled 是账本失真。")
    lines.append(
        "确实没有任何合法出口时，用 report_blocker 记下作业身份与当前观测状态再结束；"
        "直接结束会留下无人负责的开放作业。")
    lines.append(
        "注意：本闸每个 run 只拦一次，这一轮之后不会再拦。运行结束时仍有未收尾作业，"
        "会记一条持久 blocker，run 不会被判成完成。")
    try:
        ctx.state.append_transcript(
            "external_job_closure_gate", turn=ctx.turn,
            action="veto_finish", workflows=workflows,
        )
    except Exception:
        pass
    return [_sys("\n".join(lines))]


def output_corruption_sentinel_on_llm_response(ctx: HookContext, response) -> None:
    # 本相位只观察：框架 v3.1 起 hook 收到的是 response 的深拷贝，任何改写都会被
    # 丢弃。收尾闸已迁到 on_before_finish（见 _external_job_finish_gate）。
    content = (getattr(response, "content", "") or "").strip()
    hs = ctx.state.hook_state
    reason = _echo_lock_reason(content)
    if reason:
        next_count = hs.get(_ECHO_COUNT_KEY, 0) + 1
        if reason.startswith("repeated "):
            next_count = max(next_count, 2)
        hs[_ECHO_COUNT_KEY] = next_count
        hs[_ECHO_REASON_KEY] = reason
        # Stop feeding the most frequently echoed hook for a few turns. The
        # correction is injected by this sentinel instead.
        if "execution_control" in reason:
            hs[_ENG_SUPPRESS_UNTIL_KEY] = max(
                hs.get(_ENG_SUPPRESS_UNTIL_KEY, -1),
                ctx.turn + 4,
            )
        if (getattr(response, "finish_reason", "") == "length"
                and not getattr(response, "tool_calls", None)):
            hs[_ECHO_COUNT_KEY] = max(hs[_ECHO_COUNT_KEY], 3)
    else:
        hs[_ECHO_COUNT_KEY] = 0


def output_corruption_sentinel_on_turn_start(ctx: HookContext) -> list | None:
    hs = ctx.state.hook_state
    if (hs.get(_ECHO_COUNT_KEY, 0) >= 2
            and ctx.turn - hs.get(_ECHO_LAST_INJECT_KEY, -999) >= _CORRUPT_COOLDOWN):
        hs[_ECHO_LAST_INJECT_KEY] = ctx.turn
        return [_sys(
            "[corruption_sentinel] repetition_lock detected. Do not quote hook/system blocks. "
            f"reason={hs.get(_ECHO_REASON_KEY, 'unknown')}. "
            "Immediately call the next appropriate tool, or if no tool can make progress, "
            "write and freeze an experiment_log explaining the blocker with verdict=inconclusive/smoke."
        )]
    return None


output_corruption_sentinel = LoopHook(
    name="output_corruption_sentinel",
    description="LLM 输出损坏通用哨兵：参数垃圾/类型错误/异常 token/复读锁死，实证触发+冷却",
    on_turn_start=output_corruption_sentinel_on_turn_start,
    on_llm_response=output_corruption_sentinel_on_llm_response,
    on_turn_end=output_corruption_sentinel_on_turn_end,
)
register_loop_hook(output_corruption_sentinel)


# ─────────────────────────────────────────────
# high_risk_command_audit —— 高危命令机械对账（on_end）
# ─────────────────────────────────────────────
#
# 背景：一次技术性 incomplete run 暴露出质量门的可观测性缺口：
# no_unauthorized_high_risk_commands 这条 quality_check 由 LLM judge 判，但
# state_summary 只给 judge 工具调用计数，judge 根本看不到命令内容 —— 判定
# 纯靠推断；judge 输出 JSON 再一截断就直接技术性 fail。
#
# 本 hook 在 run 结束、quality_checks 之前机械对账（沿用 citation_integrity
# 的模式：hook 算，judge 只读结论）：
#   1. 重扫 transcript 全部 run_bash / execute_python 命令串
#      （复用 safe_bash.match_high_risk，与执行层拦截同一份 pattern，零漂移）；
#   2. 对每处命中，用 safe_bash 落的 highrisk_bash_blocked / allowed 事件对账：
#        blocked  → 事前拦截未执行，不算违规
#        allowed  → 已有授权放行（authorized_by 记录来源）
#        无事件   → unaccounted（绕过了执行层，真违规）
#   3. 结果写一条 observation memory（state_summary 一定会带给 judge），
#      quality_check 只需读这一行 audit 结论。

def _hr_match(cmd: str) -> str | None:
    """复用 safe_bash 的高危 pattern（模块解析见 _safe_bash）。"""
    return _safe_bash().match_high_risk(cmd)


def _iter_transcript_events(state: Any) -> list[dict]:
    events: list[dict] = []
    try:
        path = state.transcript_path
        if not path.exists():
            return events
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return events


def high_risk_command_audit_on_end(ctx: HookContext, loop_result: Any) -> None:
    """机械扫描本 run 全部命令串，把对账结论写入 memory 供 quality_check 判定。"""
    state = ctx.state
    events = _iter_transcript_events(state)

    # 对账事件来源两层（v3.2 起）：
    #   节点层 safe_bash：highrisk_{bash,python}_{blocked,allowed}
    #   框架层 dangerous_commands（PR #97）：
    #     highrisk_{bash,python}_blocked_pending_confirm（pause 未执行）
    #     highrisk_{bash,python}_confirmed_run（人工批准后执行）
    #     highrisk_{bash,python}_bypass（bypass 模式执行，留痕）
    _blocked_events = {
        "highrisk_bash_blocked", "highrisk_python_blocked",
        "highrisk_bash_blocked_pending_confirm",
        "highrisk_python_blocked_pending_confirm",
    }
    _allowed_events = {
        "highrisk_bash_allowed": None,                       # authorized_by 字段里
        "highrisk_python_allowed": None,
        "highrisk_bash_confirmed_run": "framework_hitl_confirm",
        "highrisk_python_confirmed_run": "framework_hitl_confirm",
        "highrisk_bash_bypass": "framework_bypass_mode",
        "highrisk_python_bypass": "framework_bypass_mode",
    }

    def _ev_preview(e: dict) -> str:
        return e.get("cmd_preview") or e.get("code_preview") or ""

    blocked_previews = {_ev_preview(e) for e in events
                        if e.get("event") in _blocked_events}
    allowed: dict[str, str] = {}
    for e in events:
        src = _allowed_events.get(e.get("event", ""), "__miss__")
        if src == "__miss__":
            continue
        allowed[_ev_preview(e)] = e.get("authorized_by") or src or "?"

    n_scanned = 0
    hits_blocked: list[str] = []
    hits_allowed: list[str] = []
    hits_unaccounted: list[str] = []
    for e in events:
        if e.get("event") != "tool_call" or e.get("name") not in (
                _BASH_TOOL_NAMES | _PY_TOOL_NAMES):
            continue
        args = e.get("args") or {}
        cmd = args.get("command") or args.get("code") or ""
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        n_scanned += 1
        # execute_python 的破坏命令通常在字符串字面量里（os.system('rm -rf …')），
        # 引号会挡住 shell 边界正则 —— 归一成空格再扫。
        scan_target = (cmd.replace('"', " ").replace("'", " ")
                       if e.get("name") in _PY_TOOL_NAMES else cmd)
        label = _hr_match(scan_target)
        if not label:
            continue
        preview = cmd[:200]
        if preview in blocked_previews:
            hits_blocked.append(f"{label}: {preview[:80]}")
        elif preview in allowed:
            hits_allowed.append(f"{label} (授权:{allowed[preview]}): {preview[:80]}")
        else:
            hits_unaccounted.append(f"{label}: {preview[:80]}")

    verdict = "PASS" if not hits_unaccounted else "VIOLATION"
    parts = [
        f"high_risk_command_audit: {verdict}。",
        f"机械扫描 {n_scanned} 条 run_bash/execute_python 命令串"
        f"（pattern 同 safe_bash 执行层拦截）。",
        f"高危命中：{len(hits_blocked) + len(hits_allowed) + len(hits_unaccounted)} 处"
        f"（事前拦截未执行 {len(hits_blocked)}，已授权放行 {len(hits_allowed)}，"
        f"未经授权直接执行 {len(hits_unaccounted)}）。",
    ]
    if hits_unaccounted:
        parts.append("违规明细：" + "; ".join(hits_unaccounted[:5]))
    if hits_allowed:
        parts.append("授权明细：" + "; ".join(hits_allowed[:5]))
    # transcript 事件**先落**：它不依赖记忆层，是这次对账发生过的独立证据。
    # 之前放在 try 里跟手册写入共命运 —— 手册写不成连"审过了"都查不到。
    state.append_transcript(
        "high_risk_command_audit",
        verdict=verdict, n_scanned=n_scanned,
        n_blocked=len(hits_blocked), n_allowed=len(hits_allowed),
        n_unaccounted=len(hits_unaccounted),
    )
    try:
        # 落手册：`quality_checks` 读的就是这里。写一处读另一处 = 证据永远
        # 对不上 —— 这个 hook 存的正是 QC 的判据依据。
        from core import memory as _M

        _M.append_manual(
            state, text=" ".join(parts), section=_M.SECTION_PITFALL,
            nodes=[state.node_type], tools=["run_bash", "execute_python"],
            run_id=str(state.run_id or ""),
        )
    except Exception as e:
        # 没绑 worktree 的 run 没有项目记忆 —— QC 那边也读不到，两头一起降级，
        # 不会出现"写了却读不到"。但**必须留痕**：静默吞掉会让人以为审过了。
        log.warning("high_risk_command_audit 手册写入失败：%s", e)
        state.append_transcript(
            "high_risk_command_audit_not_persisted", reason=str(e)[:200])


high_risk_command_audit = LoopHook(
    name="high_risk_command_audit",
    description="run 结束时机械对账高危命令（复用 safe_bash pattern + 拦截/授权事件），"
                "结论写 memory 供 no_unauthorized_high_risk_commands quality_check 判定",
    on_end=high_risk_command_audit_on_end,
    emits=("high_risk_command_audit",),
)
register_loop_hook(high_risk_command_audit)


# ─────────────────────────────────────────────
# route_block_resolution —— 成功自我纠偏的路线拒绝不计入终局门禁证据（E-3）
# ─────────────────────────────────────────────
# core/executor._gate_block_evidence 按 tool_calls 记录里 result.blocker.kind
# 机械计数：同类拒绝 ≥3 次即判"力气耗在框架门上"，终局 summary 落
# gate_block_evidence（run_history.externally_caused 据此把失败记到框架账上）。
# 但 execution_route 的瞬时 mismatch 被 LLM 一两轮自动纠偏后全链成功，是
# 验收标准明确允许的行为 —— E2 实测：3 次瞬时 mismatch 修正后全链成功，
# 终局仍 blocked count=3。
#
# 本 hook 在 on_end（先于 executor 计算 gate 证据）从 transcript 纯推导消解
# 事实，不新建状态：某条 execution_route_blocked 事件之后出现了成功的
# route_step_bound + route_step_outcome（outcome ∈ success/submitted）→ 该
# 拒绝已被自我纠偏解决，把对应 tool_call 记录的 blocker 移到 resolved_blocker
# （原始拒绝事实保留，只是不再作为未解决门禁证据被计数），并追加
# execution_route_block_resolved 事件记录消解事实。transcript 里已冻结的
# execution_route_blocked 事件一字不改；从未被后续成功解决的拒绝照旧计入。

_ROUTE_RESOLVED_OUTCOMES = {"success", "submitted"}

# 这几类拒绝 enforce_execution_route 不会（也不该）追加 execution_route_blocked
# 事件 —— transcript 尾部本身不安全。它们的记录与事件不再一一对应，且属
# framework 侧问题而非 LLM 可纠偏的路线绑定问题，不参与消解。
_ROUTE_UNAPPENDED_BLOCK_KINDS = frozenset({
    "route_transcript_tail_unwritable",
    "route_transcript_unreadable",
    "route_event_history_invalid",
})


def _resolved_route_block_event_counts(events: list[dict]) -> dict[str, int]:
    """route_step_id（无 step 的拒绝合并记在 "" 键）→ 已被后续成功解决的
    execution_route_blocked 事件数。

    消解界取**成功尝试的 route_step_bound 事件序**，不取 outcome 事件序：
    bound 在 spawn/submit 前实时写入，而 route_step_outcome 可能由崩溃恢复
    对账迟到补写（事件序晚于真实成功时刻），用 outcome 作界会误赦免其间新
    出现且从未纠偏的拒绝。顺序执行下 bound 与其 outcome 之间不会插入其他
    动作的事件，两种界等价，此选择只收紧崩溃+对账交错的边角。

    带 route_step_id 的拒绝按同 step 匹配。不带 route_step_id 的拒绝
    （execution_route_required / route_step_id_required / route_step_ambiguous
    等 —— 拒绝时还不存在可引用的步骤）以"其后出现了任一成功绑定并执行的
    路线步骤"为消解证据：这类拒绝的标准纠偏是声明/修订路线后经正确入口
    重试，重试动作必然携带新的 route_step_id（action_signature 含该字段，
    哈希随之改变），transcript 上无法按签名对回原拒绝；能机械对上的只有
    "路线机器随后真的走通了"这一事实。
    """
    bound_index: dict[tuple[str, str], int] = {}
    for idx, e in enumerate(events):
        if e.get("event") != "route_step_bound":
            continue
        step_id = str(e.get("route_step_id") or "")
        attempt_id = str(e.get("attempt_id") or "")
        if step_id and attempt_id:
            bound_index[(step_id, attempt_id)] = idx
    step_cutoff: dict[str, int] = {}
    any_cutoff = -1
    for e in events:
        if e.get("event") != "route_step_outcome":
            continue
        if str(e.get("outcome") or "") not in _ROUTE_RESOLVED_OUTCOMES:
            continue
        step_id = str(e.get("route_step_id") or "")
        attempt_id = str(e.get("attempt_id") or "")
        idx = bound_index.get((step_id, attempt_id))
        if idx is None:
            continue
        step_cutoff[step_id] = max(step_cutoff.get(step_id, -1), idx)
        any_cutoff = max(any_cutoff, idx)
    resolved: dict[str, int] = {}
    for idx, e in enumerate(events):
        if e.get("event") != "execution_route_blocked":
            continue
        step_id = str(e.get("route_step_id") or "")
        cutoff = step_cutoff.get(step_id, -1) if step_id else any_cutoff
        if idx < cutoff:
            resolved[step_id] = resolved.get(step_id, 0) + 1
    return resolved


def route_block_resolution_on_end(ctx: HookContext, loop_result: Any) -> None:
    records = list(getattr(loop_result, "tool_calls", None) or [])
    blocked_results_by_step: dict[str, list[dict]] = {}
    for r in records:
        if not isinstance(r, dict):
            continue
        result = r.get("result")
        if not (isinstance(result, dict) and result.get("status") == "error"
                and result.get("route_blocked")):
            continue
        blocker = result.get("blocker")
        if not (isinstance(blocker, dict) and blocker.get("kind")):
            continue
        if str(blocker.get("kind")) in _ROUTE_UNAPPENDED_BLOCK_KINDS:
            continue
        step_id = str(blocker.get("route_step_id") or "")
        blocked_results_by_step.setdefault(step_id, []).append(result)
    if not blocked_results_by_step:
        return
    resolved_counts = _resolved_route_block_event_counts(
        _iter_transcript_events(ctx.state))
    resolved_records: dict[str, int] = {}
    for step_id, results in blocked_results_by_step.items():
        # 同键拒绝记录与 execution_route_blocked 事件同序，且被消解的事件必
        # 是前缀（消解界是单一事件序号，其前的都算、其后的都不算）——
        # 消解前 n 条即可，之后的照旧计入。
        n = min(resolved_counts.get(step_id, 0), len(results))
        for result in results[:n]:
            result["resolved_blocker"] = result.pop("blocker")
            result["route_block_resolved"] = {
                "route_step_id": step_id or None,
                "resolved_by": "subsequent_successful_route_step",
            }
        if n:
            resolved_records[step_id] = n
    if not resolved_records:
        return
    ctx.state.append_transcript(
        "execution_route_block_resolved",
        resolved=resolved_records,
    )


route_block_resolution = LoopHook(
    name="route_block_resolution",
    description="run 结束时从 transcript 纯推导消解已被后续成功步骤解决的 "
                "execution_route 拒绝，成功的自我纠偏不再被终局门禁记为未解决阻断",
    on_end=route_block_resolution_on_end,
    emits=("execution_route_block_resolved",),
)
register_loop_hook(route_block_resolution)


# ─────────────────────────────────────────────
# preprocessing_boundary_audit —— 前处理边界机械对账（on_end）
# ─────────────────────────────────────────────
#
# 前处理边界不能由 LLM 从摘要猜测：它必须对本 run 的实际工具调用机械扫描。
# 结论先作为 transcript receipt 落地，供 closure、review 与事后诊断直接读取。
#
# 判定口径与白名单见 tools/preprocessing_boundary.py 的模块 docstring。
# 只观察不拦截：误判面宽，硬门禁的期望损失高于收益。

def _preprocessing_boundary():
    """惰性解析 preprocessing_boundary 模块（两种模块名都可能已登记）。"""
    for name in ("nodes.experiment.tools.preprocessing_boundary",
                 "tools.preprocessing_boundary"):
        module = sys.modules.get(name)
        if module is not None:
            return module
    import importlib
    for name in ("nodes.experiment.tools.preprocessing_boundary",
                 "tools.preprocessing_boundary"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ImportError("preprocessing_boundary 模块不可用")


def preprocessing_boundary_audit_on_end(ctx: HookContext, loop_result: Any) -> None:
    """机械扫描本 run 的前处理产物生成行为，结论作为 receipt 落 transcript。"""
    state = ctx.state
    try:
        report = _preprocessing_boundary().scan_preprocessing_boundary(state)
    except Exception as e:
        # 扫描失败也必须留下事件：mechanical fastpath 在事件缺席时判权威 fail，
        # 静默返回等于把每个扫描异常变成一次"违规"判决。
        log.warning("preprocessing_boundary_audit 扫描失败：%s", e)
        report = {"verdict": "PASS", "n_scanned": 0, "n_hits": 0,
                  "n_whitelisted": 0, "n_unaccounted": 0,
                  "accounting_reasons": ["audit_scan_failed"],
                  "scan_error": f"{type(e).__name__}: {e}"[:200]}

    # 事件先落、memory 后写：high_risk_command_audit 把 append_transcript 放在
    # memory 写入之后的同一个 try 里，memory 一抛异常事件就没了 → QC fail-closed。
    # 同样的顺序在这里不能再来一次。
    try:
        state.append_transcript(
            "preprocessing_boundary_audit",
            verdict=report.get("verdict"),
            n_scanned=report.get("n_scanned", 0),
            n_hits=report.get("n_hits", 0),
            n_whitelisted=report.get("n_whitelisted", 0),
            n_unaccounted=report.get("n_unaccounted", 0),
            accounting_reasons=report.get("accounting_reasons") or [],
            hits=report.get("hits") or [],
        )
    except Exception as e:
        log.warning("preprocessing_boundary_audit 事件写入失败：%s", e)
        return

    if not report.get("n_unaccounted", 0):
        return

    parts = [
        f"preprocessing_boundary_audit: {report.get('verdict')}。",
        f"机械扫描 {report.get('n_scanned', 0)} 条命令/写文件调用，"
        f"前处理生成命中 {report.get('n_hits', 0)} 处"
        f"（另有 {report.get('n_whitelisted', 0)} 处按运行化白名单排除）。",
    ]
    parts.append("这些命中仅是按文件名和生成器命令做的机械匹配，"
                 "不等于已确认的错误。")
    if report.get("accounting_reasons"):
        parts.append("已对账依据：" + "、".join(report["accounting_reasons"]) + "。")
    if report.get("n_unaccounted"):
        parts.append("未对账明细：" + "; ".join(
            f"{h.get('label')}: {h.get('evidence', '')[:80]}"
            for h in (report.get("hits") or [])[:5]))
    try:
        from core import memory as _M

        _M.append_manual(
            state, text=" ".join(parts), section=_M.SECTION_PITFALL,
            nodes=[state.node_type], tools=["safe_run_bash", "safe_write_file"],
            run_id=str(state.run_id or ""),
        )
    except Exception as e:
        log.warning("preprocessing_boundary_audit memory 写入失败：%s", e)


preprocessing_boundary_audit = LoopHook(
    name="preprocessing_boundary_audit",
    description="run 结束时机械对账前处理产物生成行为（生成器命令 + 正式输入文件写入，"
                "扣除运行化白名单），结论供 closure/review 与事后审计读取",
    on_end=preprocessing_boundary_audit_on_end,
    emits=("preprocessing_boundary_audit",),
)
register_loop_hook(preprocessing_boundary_audit)

# ─────────────────────────────────────────────
# external_job_handoff —— long-running external job handoff (on_end)
# ─────────────────────────────────────────────
# This is intentionally domain-neutral: a job submission is an operational
# fact, while interpreting its outputs remains the responsibility of a later
# experiment run.  Do not add solver-specific completion heuristics here.

try:
    from tools.resource_manager import (
        SUBMISSION_LEDGER_FAILED_CHECK,
        SubmissionLedgerError,
        owed_external_job_closure_records,
        read_owed_submission_receipts,
        submission_ledger_failure_reason,
    )
except ImportError:
    from .tools.resource_manager import (
        SUBMISSION_LEDGER_FAILED_CHECK,
        SubmissionLedgerError,
        owed_external_job_closure_records,
        read_owed_submission_receipts,
        submission_ledger_failure_reason,
    )


def _read_submission_records_strict(state: Any) -> list[dict[str, Any]]:
    """Compatibility wrapper for the single resource-manager ledger reader."""
    return read_owed_submission_receipts(state)


def _job_submission_records(state: Any) -> list[dict[str, Any]]:
    """Compatibility wrapper for the single read-only owed-closure judgement."""
    return owed_external_job_closure_records(state)


def _external_job_key(state: Any, submission: dict[str, Any]) -> str:
    """Task identity mirrors the manager scope identity while preserving legacy ids."""
    origin_run_id = str(
        submission.get("submitted_by_run_id")
        or submission.get("_receipt_produced_by_run_id")
        or state.run_id
    )
    base = ":".join((
        origin_run_id, str(submission.get("scheduler") or ""),
        str(submission.get("job_id") or submission.get("workdir") or ""),
    ))
    scope = {
        "namespace": str(submission.get("namespace") or ""),
        "launch_host": str(submission.get("launch_host") or ""),
        "scheduler_cluster": str(submission.get("scheduler_cluster") or ""),
        "resource_uid": str(submission.get("resource_uid") or ""),
        "submission_nonce": str(submission.get("submission_nonce") or ""),
        "container_runtime_id": str(submission.get("container_runtime_id") or ""),
    }
    return base if not any(scope.values()) else base + ":scope=" + json.dumps(
        scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _classify_external_job_status(submission: dict[str, Any]) -> str:
    """Return running, finished_or_unavailable, or unknown after one query."""
    try:
        try:
            from tools.resource_manager import _job_status_sync
        except ImportError:
            from .tools.resource_manager import _job_status_sync
        result = _job_status_sync(
            str(submission["scheduler"]), str(submission["job_id"]),
            submission.get("namespace"), remote_host=submission.get("launch_host"),
            container_runtime_id=submission.get("container_runtime_id"),
        )
    except Exception:
        return "unknown"

    raw = result.get("raw") if isinstance(result, dict) else {}
    if not isinstance(raw, dict):
        return "unknown"
    scheduler = str(submission.get("scheduler") or "").lower()
    stdout = str(raw.get("stdout") or "")
    if scheduler == "local":
        # 精确匹配，不能用子串：「RUNNING」是「NOT_RUNNING」的子串，原来先判
        # `"RUNNING" in stdout` 会把每个已结束的本地作业都判成 running，下面判
        # NOT_RUNNING 的那行永远走不到。于是 run 收尾时交接账本里写的是「作业仍在
        # 运行」这一假事实（2026-09-10 验收 2 活体实测）。与 resource_manager 里
        # 同一判断的另外三处写法对齐，都是 stdout.strip() 精确比较。
        value = stdout.strip()
        if value == "RUNNING":
            return "running"
        if value == "NOT_RUNNING":
            return "finished_or_unavailable"
        return "unknown"
    if scheduler == "slurm" and raw.get("ok"):
        return "running" if stdout.strip() else "finished_or_unavailable"
    if scheduler == "pbs" and raw.get("ok"):
        return ("finished_or_unavailable"
                if re.search(r"job_state\s*=\s*[CEF]\b", stdout)
                else "running")
    if scheduler == "kubernetes" and raw.get("ok"):
        try:
            status = json.loads(stdout).get("status") or {}
        except (TypeError, json.JSONDecodeError):
            return "unknown"
        if status.get("active"):
            return "running"
        if status.get("succeeded") or status.get("failed"):
            return "finished_or_unavailable"
    return "unknown"


def _external_execution_class(submission: dict[str, Any]) -> str:
    """Read the new derived class while remaining able to resume old receipts."""
    value = str(
        submission.get("execution_class") or submission.get("stage") or "diagnostic"
    ).strip().lower()
    return value if value in {
        "diagnostic", "toolchain_build", "simulation",
    } else "diagnostic"


def _ensure_external_job_task(state: Any, submission: dict[str, Any], key: str) -> str | None:
    if not state.project_root:
        return None
    try:
        from core.tasks import TaskList
        tasks = TaskList(state.project_root / "tasks")
        marker = f"external_job_key={key}"
        existing_task_id = str(submission.get("task_id") or "")
        for task in tasks.list_all():
            if (existing_task_id and task.id == existing_task_id
                    or marker in task.description):
                return task.id
        description = "\n".join((
            marker,
            "Verify and finalize this external job in a later experiment run.",
            f"submitted_by_run_id={state.run_id}",
            f"submitted_at={submission.get('submitted_at') or ''}",
            "experiment_workflow_status=awaiting_external_job",
            f"scheduler={submission.get('scheduler')}",
            f"job_id={submission.get('job_id')}",
            f"namespace={submission.get('namespace') or ''}",
            f"scheduler_cluster={submission.get('scheduler_cluster') or ''}",
            f"resource_uid={submission.get('resource_uid') or ''}",
            f"submission_nonce={submission.get('submission_nonce') or ''}",
            f"container_runtime_id={submission.get('container_runtime_id') or ''}",
            f"launch_host={submission.get('launch_host') or ''}",
            f"remote_hosts={json.dumps(submission.get('remote_hosts') or [], ensure_ascii=False)}",
            f"remote_marker_path={submission.get('remote_marker_path') or ''}",
            f"workdir={submission.get('workdir') or ''}",
            f"output_dir={submission.get('scheduler_output_dir') or ''}",
            f"stdout_path={submission.get('stdout_path') or ''}",
            f"stderr_path={submission.get('stderr_path') or ''}",
            f"health_contract={json.dumps(submission.get('health_contract') or {}, ensure_ascii=False, sort_keys=True)}",
            f"execution_class={_external_execution_class(submission)}",
            # 兼容旧 continuation reader；值来自内部派生，不是 caller stage。
            f"stage={_external_execution_class(submission)}",
            f"expected_duration_s={submission.get('expected_duration_s') or ''}",
            f"output_roots={json.dumps(submission.get('output_roots') or [], ensure_ascii=False)}",
            "The job being absent is not evidence of success; inspect outputs before a verdict.",
        ))
        task = tasks.create(
            title=f"Finalize external job: {submission.get('job_name') or submission.get('job_id')}",
            description=description,
            owner_node=state.node_type,
            run_id=state.run_id,
        )
        return task.id
    except Exception as exc:
        log.warning("external job handoff task failed: %s", exc)
        return None



def persist_external_job_workflow(state: Any, submission: dict[str, Any]) -> str | None:
    """Persist recoverable workflow state immediately after a real submission.

    ``on_end`` is only a reconciliation point: a model empty-stop, provider
    failure, user interrupt, or process crash can occur before it runs.
    """
    if (submission.get("status") != "success" or submission.get("dry_run")
            or not submission.get("scheduler") or not submission.get("job_id")):
        return None
    scheduler = str(submission["scheduler"]).lower()
    job_id = str(submission["job_id"])
    key = _external_job_key(state, submission)
    workflow = {
        "external_job_key": key,
        "scheduler": scheduler,
        "job_id": job_id,
        "namespace": submission.get("namespace"),
        "launch_host": submission.get("launch_host"),
        "scheduler_cluster": submission.get("scheduler_cluster"),
        "resource_uid": submission.get("resource_uid"),
        "submission_nonce": submission.get("submission_nonce"),
        "container_runtime_id": submission.get("container_runtime_id"),
        "submitted_by_run_id": state.run_id,
        "workdir": submission.get("workdir"),
        "output_roots": submission.get("output_roots") or [],
        "execution_class": _external_execution_class(submission),
        "stage": _external_execution_class(submission),
        "expected_duration_s": submission.get("expected_duration_s"),
        "health_contract": submission.get("health_contract") or {},
        "workflow_status": "awaiting_external_job",
        "review_eligibility": False,
    }
    try:
        state.save_artifact(
            "external_job_workflow",
            (f"external_job_workflow_{state.run_id}_"
             + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
             + f"_{scheduler}_{job_id}"),
            json.dumps(workflow, ensure_ascii=False, indent=2),
            metadata={"scheduler": scheduler, "job_id": job_id,
                      "namespace": submission.get("namespace"),
                      "launch_host": submission.get("launch_host"),
                      "scheduler_cluster": submission.get("scheduler_cluster"),
                      "resource_uid": submission.get("resource_uid"),
                      "submission_nonce": submission.get("submission_nonce"),
                      "container_runtime_id": submission.get("container_runtime_id"),
                      "workflow_status": workflow["workflow_status"]},
        )
    except Exception:
        log.warning("unable to persist external job workflow artifact", exc_info=True)
    task_id = _ensure_external_job_task(state, submission, key)
    try:
        state.hook_state["external_job_waiting"] = {
            "node_execution_status": "suspended_for_external_wait",
            "experiment_workflow_status": "awaiting_external_job",
            "scientific_result_status": "awaiting_external_job",
            "assessment_status": "not_available",
            "open_external_jobs": [workflow],
            "follow_up_tasks": [task_id] if task_id else [],
            "review_eligibility": False,
        }
    except Exception:
        pass
    try:
        state.append_transcript("external_job_workflow_persisted", **workflow,
                                task_id=task_id)
    except Exception:
        pass
    return task_id


def _identity_recovery_key(state: Any, submission: dict[str, Any]) -> str:
    """Stable recovery case identity, distinct from a normal scheduler job key."""
    material = {
        "run_id": str(getattr(state, "run_id", "") or ""),
        "scheduler": str(submission.get("scheduler") or "").lower(),
        "namespace": str(submission.get("namespace") or ""),
        "job_name": str(submission.get("job_name") or ""),
        "submitted_at": str(submission.get("submitted_at") or ""),
        "submission_nonce": str(submission.get("submission_nonce") or ""),
        "script_sha256": str(submission.get("script_sha256") or ""),
    }
    return "external_job_identity_recovery:" + hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def _ensure_external_job_identity_recovery_task(
    state: Any, submission: dict[str, Any], key: str,
) -> str | None:
    if not state.project_root:
        return None
    try:
        from core.tasks import TaskList
        tasks = TaskList(Path(state.project_root) / "tasks")
        marker = f"external_job_identity_recovery_key={key}"
        for task in tasks.list_all():
            if marker in task.description:
                return task.id
        description = "\n".join((
            marker,
            "Recover the scheduler identity for an accepted external job before any resubmit.",
            "experiment_workflow_status=accepted_identity_unresolved",
            f"submitted_by_run_id={state.run_id}",
            f"scheduler={submission.get('scheduler') or ''}",
            f"namespace={submission.get('namespace') or ''}",
            f"job_name={submission.get('job_name') or ''}",
            f"submitted_at={submission.get('submitted_at') or ''}",
            f"submission_nonce={submission.get('submission_nonce') or ''}",
            f"script_path={submission.get('script_path') or ''}",
            f"script_sha256={submission.get('script_sha256') or ''}",
            f"submit_stdout={str((submission.get('submit_result') or {}).get('stdout') or '')[:1000]}",
            "do_not_resubmit=true",
        ))
        task = tasks.create(
            title=("Recover external job identity: "
                   + str(submission.get("job_name") or submission.get("scheduler") or "unknown")),
            description=description, owner_node=state.node_type, run_id=state.run_id,
        )
        return task.id
    except Exception as exc:
        log.warning("external job identity recovery task failed: %s", exc)
        return None


def persist_external_job_identity_recovery(state: Any, submission: dict[str, Any]) -> str | None:
    """Persist an accepted no-ID submission as recovery-only, never finalizable work."""
    if (submission.get("status") not in {
                "accepted_identity_unresolved", "submission_outcome_unknown",
            }
            or submission.get("dry_run")
            or not submission.get("scheduler")
            or submission.get("job_id") not in {None, ""}
            or not submission.get("do_not_resubmit")):
        return None
    key = _identity_recovery_key(state, submission)
    workflow = {
        "external_job_identity_recovery_key": key,
        "scheduler": str(submission.get("scheduler") or "").lower(),
        "namespace": submission.get("namespace"),
        "job_name": submission.get("job_name"),
        "submitted_at": submission.get("submitted_at"),
        "submission_nonce": submission.get("submission_nonce"),
        "script_path": submission.get("script_path"),
        "script_sha256": submission.get("script_sha256"),
        "submit_result": submission.get("submit_result") or {},
        "workflow_status": submission.get("status"),
        "do_not_resubmit": True,
        "review_eligibility": False,
    }
    try:
        state.save_artifact(
            "external_job_identity_recovery_workflow",
            "external_job_identity_recovery_" + hashlib.sha256(
                key.encode("utf-8")).hexdigest()[:16],
            json.dumps(workflow, ensure_ascii=False, indent=2),
            metadata={"scheduler": workflow["scheduler"],
                      "namespace": workflow["namespace"],
                      "workflow_status": workflow["workflow_status"]},
        )
    except Exception:
        log.warning("unable to persist external job identity recovery artifact", exc_info=True)
    task_id = _ensure_external_job_identity_recovery_task(state, submission, key)
    try:
        previous = state.hook_state.get("external_job_identity_recovery") or {}
        previous_cases = [
            item for item in (previous.get("cases") or [])
            if item.get("external_job_identity_recovery_key") != key
        ]
        previous_tasks = [
            str(item) for item in (previous.get("follow_up_tasks") or [])
            if str(item)
        ]
        if task_id and task_id not in previous_tasks:
            previous_tasks.append(task_id)
        state.hook_state["external_job_identity_recovery"] = {
            "workflow_status": workflow["workflow_status"],
            "review_eligibility": False,
            "do_not_resubmit": True,
            "cases": [*previous_cases, workflow],
            "follow_up_tasks": previous_tasks,
        }
    except Exception:
        pass
    try:
        state.append_transcript("external_job_identity_recovery_persisted", **workflow,
                                task_id=task_id)
    except Exception:
        pass
    return task_id


def _record_open_external_job_blocker(
    state: Any,
    loop_result: Any,
    handoffs: list[dict[str, Any]],
) -> None:
    """Persist one Core blocker for every exact unresolved job identity."""
    try:
        from tools.resource_manager import (
            _ensure_blocker_list_for_recording,
            _external_job_handoff_reported_by,
        )
    except ImportError:
        from .tools.resource_manager import (
            _ensure_blocker_list_for_recording,
            _external_job_handoff_reported_by,
        )
    from core.blockers import record_blocker

    blockers = _ensure_blocker_list_for_recording(state)
    for handoff in handoffs:
        marker = _external_job_handoff_reported_by(handoff)
        if any(
            isinstance(item, dict) and item.get("reported_by") == marker
            for item in blockers
        ):
            continue
        blocker = record_blocker(
            state,
            summary=(
                "external experiment job workflow remains open: "
                + str(handoff.get("scheduler")) + ":"
                + str(handoff.get("job_id"))
            ),
            category="external_job",
            requested_action=(
                "resume Experiment, inspect terminal output, freeze the "
                "experiment_log, then call finalize_external_job"
            ),
            suggested_owner="experiment",
            retryable_after_change=True,
            reported_by=marker,
        )
        blocker.update({
            "reason": "external_job_handoff_open",
            "scheduler": str(handoff.get("scheduler") or "").lower(),
            "job_id": str(handoff.get("job_id") or ""),
            "namespace": handoff.get("namespace"),
            "submission_nonce": handoff.get("submission_nonce"),
            "container_runtime_id": handoff.get("container_runtime_id"),
        })
    if str(getattr(loop_result, "status", "") or "") not in {"cancelled", "paused"}:
        try:
            loop_result.status = "blocked"
        except Exception:
            pass


def external_job_handoff_on_end(ctx: HookContext, loop_result: Any) -> None:
    """Persist every unfinalized submission as an open experiment workflow.

    A scheduler terminal state is deliberately awaiting_analysis, not success: a
    later experiment continuation must inspect outputs and finalize the handoff.
    """
    state = ctx.state
    try:
        submissions = _job_submission_records(state)
    except Exception as exc:
        # The submission artifact ledger is the authority for whether an
        # external workflow remains open.  Treat an unreadable ledger as a
        # closure failure instead of letting run_on_end swallow this hook
        # exception and allowing Core to write a false completed summary.
        _block_experiment_completion(
            state, loop_result,
            blocker_id="experiment_job_submission_read_error",
            reason=submission_ledger_failure_reason(exc),
            failed_checks=[SUBMISSION_LEDGER_FAILED_CHECK],
            summary=("unable to read authoritative external job submission "
                     "records; cannot verify that external work is finalized"),
        )
        log.warning("unable to read external job submission records during handoff: %s",
                    type(exc).__name__)
        return

    handoffs: list[dict[str, Any]] = []
    for submission in submissions:
        key = str(submission.get("external_job_key") or
                  _external_job_key(state, submission))
        try:
            from core.cancellation import signal_for
            cancelled = bool(signal_for(state))
        except Exception:
            cancelled = False
        # Cancellation must not wait for a scheduler CLI. Preserve this job as
        # unknown/open and let a later Experiment run reconcile it.
        status = "unknown" if cancelled else _classify_external_job_status(submission)
        task_id = _ensure_external_job_task(state, submission, key)
        workflow_status = ("awaiting_external_job"
                           if status in {"running", "unknown"}
                           else "awaiting_analysis")
        handoffs.append({
            "external_job_key": key, "artifact_id": submission.get("artifact_id"),
            "scheduler": submission.get("scheduler"), "job_id": submission.get("job_id"),
            "namespace": submission.get("namespace"),
            "launch_host": submission.get("launch_host"),
            "scheduler_cluster": submission.get("scheduler_cluster"),
            "resource_uid": submission.get("resource_uid"),
            "submission_nonce": submission.get("submission_nonce"),
            "process_group_id": submission.get("process_group_id"),
            "process_start_ticks": submission.get("process_start_ticks"),
            "container_runtime_id": submission.get("container_runtime_id"),
            "job_name": submission.get("job_name"), "status": status, "task_id": task_id,
            "workflow_status": workflow_status, "workdir": submission.get("workdir"),
            "output_dir": submission.get("scheduler_output_dir"),
            "output_roots": submission.get("output_roots") or [],
            "execution_class": _external_execution_class(submission),
            "stage": _external_execution_class(submission),
            "expected_duration_s": submission.get("expected_duration_s"),
            "health_contract": submission.get("health_contract") or {},
        })
    if not handoffs:
        return
    _record_open_external_job_blocker(state, loop_result, handoffs)
    scientific_result_status = ("awaiting_external_job"
                                if any(item["workflow_status"] == "awaiting_external_job" for item in handoffs)
                                else "awaiting_analysis")
    for handoff in handoffs:
        state.append_transcript(
            "external_job_handoff", **handoff,
            scientific_result_status=scientific_result_status,
            review_eligibility=False,
        )
    waiting = {
        "node_execution_status": "suspended_for_external_wait",
        "experiment_workflow_status": scientific_result_status,
        "scientific_result_status": scientific_result_status,
        "assessment_status": "not_available",
        "open_external_jobs": handoffs,
        "follow_up_tasks": [item["task_id"] for item in handoffs if item.get("task_id")],
        "review_eligibility": False,
    }
    try:
        state.hook_state["external_job_waiting"] = waiting
    except Exception:
        pass
    current = str(getattr(loop_result, "final_text", "") or "")
    footer = "\n\n## External Job Workflow\n"
    footer += "- The current agent invocation ended; the experiment workflow remains open.\n"
    footer += "- experiment_workflow_status: " + scientific_result_status + "\n"
    footer += "- Follow-up task(s): " + ", ".join(item["task_id"] or "not-created" for item in handoffs) + ".\n"
    footer += "- review_eligibility: false\n"
    footer += "- You may exit chat safely. Re-enter the project to check health; after scheduler termination, the same experiment must inspect outputs and finalize analysis before any verdict or review.\n"
    footer += "- This is operational workflow state, not scientific evidence or a verdict."
    if "## External Job Workflow" not in current:
        try:
            loop_result.final_text = current + footer
        except Exception:
            log.warning("unable to append external-job workflow footer")

def _external_job_tasks_for_reconciliation(state: Any) -> list[dict[str, Any]]:
    if not getattr(state, "project_root", None):
        return []
    try:
        from core.tasks import TaskList
        tasks = TaskList(state.project_root / "tasks").list_all()
    except Exception:
        return []
    jobs: list[dict[str, Any]] = []
    for task in tasks:
        if task.status == "completed" or "external_job_key=" not in task.description:
            continue
        fields = {}
        for line in task.description.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key.strip()] = value.strip()
        if fields.get("scheduler") and fields.get("job_id"):
            try:
                health_contract = json.loads(fields.get("health_contract", "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                health_contract = {}
            jobs.append({"task_id": task.id, "scheduler": fields["scheduler"],
                         "job_id": fields["job_id"], "workdir": fields.get("workdir"),
                         "namespace": fields.get("namespace") or None,
                         "scheduler_cluster": fields.get("scheduler_cluster") or None,
                         "resource_uid": fields.get("resource_uid") or None,
                         "submission_nonce": fields.get("submission_nonce") or None,
                         "container_runtime_id": fields.get("container_runtime_id") or None,
                         "launch_host": fields.get("launch_host") or None,
                         "remote_marker_path": fields.get("remote_marker_path") or None,
                         "stdout_path": fields.get("stdout_path") or None,
                         "stderr_path": fields.get("stderr_path") or None,
                         "scheduler_output_dir": fields.get("output_dir") or None,
                         "submitted_at": fields.get("submitted_at") or None,
                         "output_roots": _parse_external_job_output_roots(fields.get("output_roots", "")),
                         "health_contract": health_contract if isinstance(health_contract, dict) else {},
                         "execution_class": _external_execution_class(fields),
                         "stage": _external_execution_class(fields),
                         "expected_duration_s": fields.get("expected_duration_s")})
    return jobs

def _parse_external_job_output_roots(value: str) -> list[str]:
    if not value.startswith("["):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed) else []


def _ledger_invalid_blocker_guidance(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Aggregate one order-independent blocker for all ledger-invalid rows."""
    invalid = [row for row in rows if row.get("status") == "ledger_invalid"]
    foreign_local = [
        row for row in invalid
        if (row.get("foreign_run") is True
            and str(row.get("scheduler") or "").strip().lower() == "local")
    ]
    foreign_nonlocal = [
        row for row in invalid
        if (row.get("foreign_run") is True
            and str(row.get("scheduler") or "").strip().lower() != "local")
    ]
    ledger_damage = [row for row in invalid if row.get("foreign_run") is not True]
    local_action = (
        "若原任务本来就要执行这次新提交，只通过正常受管 submit_job；本地作业隔离层"
        "会在新的 submission intent、route attempt 与作业 launch/外部提交前机械重探活。"
        "只有取得正面死亡证据后，该 foreign intent 才不再阻断；其他 conflicts 仍独立"
        "检查。探活 unavailable/alive 时保持拒绝，不产生新的 submission intent、route "
        "attempt 或作业 launch/外部提交。不要仅为探活调用 submit_job"
    )
    if invalid and len(foreign_local) == len(invalid):
        return (
            "其他 run 的 local 外部提交 intent 未达终态，且当前格式没有可识别的死亡收养证据",
            local_action,
        )
    if foreign_local:
        return (
            "外部提交 ledger_invalid 包含恢复条件不同的多类条目",
            "逐条处理：只有来自其他 run 且 scheduler=local 的 intent，并且原任务本来就要"
            "执行这次新提交时，才可走正常受管 submit_job；该 intent 会在新 launch 前机械重探活，"
            "只有取得正面死亡证据后该 foreign intent 才不再阻断，其他 conflicts 仍独立检查。"
            "其余 ledger_invalid 行仍禁止 submit_job。不要仅为探活调用 submit_job",
        )
    if foreign_nonlocal and ledger_damage:
        return (
            "外部提交 ledger_invalid 同时包含 non-local foreign intent 与权威账本损坏",
            "禁止 submit_job；non-local intent 没有本地作业隔离层收养通道，交由 scheduler/"
            "框架 owner 核对身份和终态；对真正损坏的账本先修复后再对账",
        )
    if foreign_nonlocal:
        return (
            "其他 run 的 non-local 外部提交 intent 未达终态，且没有本地收养通道",
            "禁止再次 submit_job；非 local intent 没有本地作业隔离层收养通道，交由 "
            "scheduler/框架 owner 核对身份和终态",
        )
    return (
        "外部提交 intent/recovery 权威账本损坏，无法证明是否已提交",
        "修复当前 Experiment run 的 submission 账本后再对账；禁止重提",
    )


def _reconcile_ledger_invalid_blocker(state: Any, rows: list[dict[str, Any]]) -> None:
    """Create/update the aggregate marker, or resolve it when no invalid row remains."""
    marker = "framework:external_submission_recovery_ledger_invalid"
    blockers = list(state.hook_state.get("blockers") or [])
    matching = [
        item for item in blockers
        if isinstance(item, dict) and item.get("reported_by") == marker
    ]
    invalid = [row for row in rows if row.get("status") == "ledger_invalid"]
    if not invalid:
        if not matching:
            return
        state.hook_state["blockers"] = [
            item for item in blockers
            if not (isinstance(item, dict) and item.get("reported_by") == marker)
        ]
        try:
            state.append_transcript(
                "blocker_resolved", reported_by=marker,
                reason="external_submission_ledger_no_longer_invalid",
            )
        except Exception:
            pass
        return

    summary, requested_action = _ledger_invalid_blocker_guidance(invalid)
    fields = {
        "category": "external_job",
        "summary": summary,
        "requested_action": requested_action,
        "suggested_owner": "framework",
        "retryable_after_change": True,
    }
    try:
        if not matching:
            from core.blockers import record_blocker
            record_blocker(state, reported_by=marker, **fields)
            return
        matching[0].update(fields)
        kept_marker = False
        deduplicated = []
        for item in blockers:
            if isinstance(item, dict) and item.get("reported_by") == marker:
                if kept_marker:
                    continue
                kept_marker = True
            deduplicated.append(item)
        state.hook_state["blockers"] = deduplicated
    except Exception:
        log.warning("unable to reconcile submission recovery ledger blocker",
                    exc_info=True)

def external_job_reconciliation_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """Surface durable workflow state before a continuation can duplicate work."""
    try:
        from tools.external_submission_recovery import dangling_external_submission_intents
    except ImportError:
        from .tools.external_submission_recovery import dangling_external_submission_intents
    dangling = dangling_external_submission_intents(ctx.state)
    jobs = _external_job_tasks_for_reconciliation(ctx.state)
    _reconcile_ledger_invalid_blocker(ctx.state, dangling)
    if not jobs and not dangling:
        return None
    snapshot_material = {
        "jobs": [{
            key: row.get(key) for key in (
                "task_id", "scheduler", "job_id", "namespace", "launch_host",
                "scheduler_cluster", "resource_uid", "submission_nonce",
                "container_runtime_id",
            )
        } for row in jobs],
        "dangling": [{
            key: row.get(key) for key in (
                "status", "route_attempt_id", "submission_nonce",
                "intent_artifact_id", "reason", "scheduler", "foreign_run",
            )
        } for row in dangling],
    }
    snapshot_key = hashlib.sha256(json.dumps(
        snapshot_material, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    if ctx.state.hook_state.get("_external_job_reconciliation_seen") == snapshot_key:
        return None
    try:
        from tools.resource_manager import probe_external_job_health
    except ImportError:
        from .tools.resource_manager import probe_external_job_health
    reports = []
    for job in jobs:
        health = probe_external_job_health(
            ctx.state, job["scheduler"], job["job_id"],
            job.get("namespace"),
        )
        reports.append({**job, "health": health,
                        "workflow_status": health.get("workflow_status", "awaiting_external_job")})
    # intent 已在不可逆提交前落盘；无 job receipt 不能解释为“没有提交”。建立现有
    # recovery workflow/task 与 blocker，恢复工具随后仍以 intent 为唯一事实源。
    for intent in dangling:
        if intent.get("status") == "ledger_invalid":
            continue
        persist_external_job_identity_recovery(ctx.state, intent)
        marker = "framework:external_job_identity_unresolved"
        blockers = list(ctx.state.hook_state.get("blockers") or [])
        def _matches_existing_identity_blocker(item: Any) -> bool:
            if not isinstance(item, dict) or item.get("reported_by") != marker:
                return False
            evidence_ids = {
                str(path) for path in (item.get("evidence_paths") or []) if str(path)
            }
            if str(intent.get("intent_artifact_id") or "") in evidence_ids:
                return True
            for artifact_id in evidence_ids:
                try:
                    outer = ctx.state.read_artifact(artifact_id)
                    payload = json.loads(str((outer or {}).get("content") or ""))
                except (AttributeError, TypeError, json.JSONDecodeError):
                    continue
                if (str(payload.get("submission_nonce") or "")
                        == str(intent.get("submission_nonce") or "")):
                    return True
            return False
        if not any(
                _matches_existing_identity_blocker(item)
                for item in blockers):
            try:
                from core.blockers import record_blocker
                record_blocker(
                    ctx.state,
                    category="external_job",
                    summary="外部提交 intent 已落盘，但 scheduler outcome/identity 尚未对账",
                    requested_action=(
                        "禁止重提；使用 reconcile_external_submission(route_attempt_id=...) "
                        "按框架 nonce 对账"
                    ),
                    suggested_owner="experiment",
                    retryable_after_change=True,
                    reported_by=marker,
                    evidence_paths=[str(intent.get("intent_artifact_id") or "")],
                )
            except Exception:
                log.warning("unable to record dangling submission intent blocker",
                            exc_info=True)
    ctx.state.hook_state["_external_job_reconciliation_seen"] = snapshot_key
    try:
        ctx.state.append_transcript(
            "external_job_reconciliation", jobs=reports,
            dangling_submission_intents=dangling,
        )
    except Exception:
        pass
    active = [row for row in reports if row["workflow_status"] == "awaiting_external_job"]
    terminal = [row for row in reports if row["workflow_status"] == "awaiting_analysis"]
    foreign_local_ledger_invalid = any(
        row.get("status") == "ledger_invalid"
        and row.get("foreign_run") is True
        and str(row.get("scheduler") or "").strip().lower() == "local"
        for row in dangling
    )
    lines = ["⏳ 本项目存在尚未关闭的 experiment workflow；这不是科学实验完成。"]
    for row in dangling:
        if row.get("status") == "ledger_invalid":
            if (row.get("foreign_run") is True
                    and str(row.get("scheduler") or "").strip().lower() == "local"):
                lines.append(
                    "- 其他 run 的 submission intent 未被当前格式的死亡收养证据覆盖："
                    f"{row.get('reason')}；历史收养记录可能使用旧探活键。本地作业隔离层"
                    "会在新的 submission intent、route attempt 与作业 launch/外部提交前"
                    "重新核验。")
            elif row.get("foreign_run") is True:
                lines.append(
                    "- 其他 run 的 submission intent 未被当前格式的死亡收养证据覆盖："
                    f"{row.get('reason')}；非 local intent 没有本地作业隔离层收养通道，"
                    "禁止再次 submit_job；交由 scheduler/框架 owner 核对身份和终态。")
            else:
                lines.append(
                    f"- submission intent 账本损坏：{row.get('reason')}；"
                    "禁止重提，先修复账本。")
            continue
        lines.append(
            f"- route_attempt={row.get('route_attempt_id')} scheduler={row.get('scheduler')} "
            "提交结果/身份未知；调用 reconcile_external_submission，仅传 route_attempt_id。"
        )
    for row in active:
        health = row["health"]
        lines.append(
            f"- task={row['task_id']} job={row['scheduler']}:{row['job_id']} "
            f"health={health.get('health_state', 'unknown')} "
            f"progress_age_s={health.get('progress_age_s')}"
        )
    for row in terminal:
        lines.append(
            f"- task={row['task_id']} job={row['scheduler']}:{row['job_id']} 已离开 scheduler；"
            "这不是成功证据。读取输出、完成分析、冻结 experiment_log 后调用 finalize_external_job。"
        )
    lines += [
        ("对上述 local 跨 run intent，若原任务本来就要执行这次新提交，只通过正常受管 "
         "submit_job；它会在新 launch 前机械重探活。只有取得正面死亡证据后，该 "
         "foreign intent 才不再阻断；其他 conflicts 仍独立检查。探活 unavailable/alive "
         "时保持拒绝，不产生新的 submission intent、"
         "route attempt 或作业 launch/外部提交。不要仅为探活调用 submit_job；不得绕过"
         "受管入口提交，也不得裸 kill。"
         if foreign_local_ledger_invalid else
         "不得向重叠 output_roots/workdir 再次 submit_job，也不得裸 kill。"),
        "仍运行时可向用户报告可安全退出 chat；会话内监控只应调用 check_external_job_health，不得用 LLM sleep/轮询。",
        "只有 finalize_external_job 完成后，该 handoff workflow 才能关闭；随后才可判 verdict。",
    ]
    return [_sys("\n".join(lines))]

external_job_reconciliation = LoopHook(
    name="external_job_reconciliation",
    description=("experiment 启动时对账未完成 external-job task 与 dangling submission "
                 "intent，防止 continuation 重提重叠输出"),
    on_turn_start=external_job_reconciliation_on_turn_start,
    emits=("external_job_reconciliation",),
)
register_loop_hook(external_job_reconciliation)

external_job_handoff = LoopHook(
    name="external_job_handoff",
    description="收尾闸否决未收尾外部作业的结束，并在 run 结束时记录一次性状态与持久跟进 task。",
    on_before_finish=_external_job_finish_gate,
    on_end=external_job_handoff_on_end,
    emits=("external_job_handoff", "external_job_closure_gate"),
)
register_loop_hook(external_job_handoff)


# ─────────────────────────────────────────────
# run_turn_start_hooks —— 测试兼容导出
# ─────────────────────────────────────────────
# 只跑 on_turn_start。名字曾叫 run_all_hooks，两个名字却都跑不起来：
# "wrf_failure_detector" 只是 generic_failure_detector 的模块级 Python 别名，
# 注册表里查不到。名单
# 必须写**注册名**，且只收 turn-start hook，缺相位/查无此名一律 warning 报出来。
_TURN_START_COMPAT_HOOKS = ("generic_failure_detector",)


def run_turn_start_hooks(ctx: HookContext) -> None:
    """手动触发 turn-start hook 的测试入口。

    实际框架运行时不使用此函数 —— 框架通过 register_loop_hook 自动调度，
    on_end 相位由 loop_hooks.run_on_end 负责，不在这里。
    """
    from core.loop_hooks import get_loop_hook

    for name in _TURN_START_COMPAT_HOOKS:
        hook = get_loop_hook(name)
        if hook is None:
            log.warning("run_turn_start_hooks: hook %r 未注册", name)
            continue
        if hook.on_turn_start is None:
            log.warning(
                "run_turn_start_hooks: hook %r 没有 on_turn_start，已跳过 —— "
                "只有 on_end 的 hook 不该出现在本名单里", name)
            continue
        try:
            hook.on_turn_start(ctx)
        except Exception as e:
            log.warning("run_turn_start_hooks: %s on_turn_start 失败: %s", name, e)
