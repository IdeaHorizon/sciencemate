"""hypothesis 节点专属 loop hook —— 运行时细节日志 + JSON 转义修复。"""
from __future__ import annotations

import json
import logging
from typing import Any

from core.llm import LLMMessage, LLMResponse
from core.loop_hooks import HookContext, LoopHook, register_loop_hook
from core import tool_registry

from nodes.hypothesis.json_tool_args import (
    normalize_prereg_metadata,
    parse_tool_arguments,
    sanitize_message_tool_calls,
)
from nodes.hypothesis.artifact_recovery import recover_missing_artifacts

log = logging.getLogger("hypothesis.runtime_trace")

# hook_state keys
_TOOL_COUNT_KEY = "runtime_trace_tool_count"
_TRUNCATED_TURN_KEY = "last_truncated_turn"
_FREEZE_CAPITAL_FAILS_KEY = "freeze_capital_basis_fail_count"
_CAPITAL_BASIS_NUDGE_KEY = "nudge_capital_basis_format"
_OVERVIEW_AFTER_FREEZE_NUDGE_KEY = "nudge_overview_after_freeze_stall"


def _brief_value(value: Any, *, max_len: int = 120) -> Any:
    """把参数/结果压成适合日志的一行预览。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.replace("\n", "\\n")
        return text if len(text) <= max_len else text[: max_len - 3] + "..."
    if isinstance(value, dict):
        return {k: _brief_value(v, max_len=max_len) for k, v in list(value.items())[:8]}
    if isinstance(value, list):
        if len(value) <= 5:
            return [_brief_value(v, max_len=max_len) for v in value]
        head = [_brief_value(v, max_len=max_len) for v in value[:3]]
        return head + [f"... +{len(value) - 3} more"]
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _tool_result_status(result: Any) -> str:
    if isinstance(result, dict):
        status = result.get("status")
        if status:
            return str(status)
        if result.get("error"):
            return "error"
        return "ok"
    return type(result).__name__


def _runtime_trace_on_turn_start(ctx: HookContext) -> None:
    max_turns = ctx.harness.max_turns
    turn_limit = "∞" if max_turns <= 0 else str(max_turns)
    log.info(
        "━━━ hypothesis loop turn %d/%s 开始 (run=%s) ━━━",
        ctx.turn,
        turn_limit,
        ctx.state.run_id,
    )


def _runtime_trace_on_llm_response(ctx: HookContext, response: LLMResponse) -> None:
    if getattr(response, "finish_reason", "") == "length":
        ctx.state.hook_state[_TRUNCATED_TURN_KEY] = ctx.turn
        log.warning(
            "turn %d: LLM 输出被 max_output_tokens 截断；"
            "若正在 save 大 artifact，请改用 stage_hypothesis_draft + save_artifact(content_from_file=...)",
            ctx.turn,
        )

    tool_calls = response.tool_calls or []
    if not tool_calls:
        log.info(
            "turn %d: LLM 完成，无 tool call（finish_reason=%s）",
            ctx.turn,
            response.finish_reason,
        )
        preview = (response.content or "").strip().replace("\n", " ")
        if preview:
            log.info("turn %d: 回复预览: %s", ctx.turn, _brief_value(preview, max_len=200))
        return

    names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
    log.info(
        "turn %d: LLM 请求 %d 个 tool → %s",
        ctx.turn,
        len(tool_calls),
        ", ".join(names),
    )
    for idx, tc in enumerate(tool_calls, start=1):
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "?")
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            args = {"_raw": raw_args}
        log.info(
            "turn %d:   [%d/%d] 准备调用 %s args=%s",
            ctx.turn,
            idx,
            len(tool_calls),
            name,
            _brief_value(args),
        )


def _runtime_trace_on_turn_end(ctx: HookContext) -> None:
    records = ctx.tool_call_records or []
    if not records:
        return

    total = int(ctx.state.hook_state.get(_TOOL_COUNT_KEY, 0))
    for rec in records:
        total += 1
        name = rec.get("name", "?")
        status = _tool_result_status(rec.get("result"))
        log.info(
            "turn %d: tool #%d 完成 %s → %s result=%s",
            ctx.turn,
            total,
            name,
            status,
            _brief_value(rec.get("result")),
        )
    ctx.state.hook_state[_TOOL_COUNT_KEY] = total


def _runtime_trace_on_end(ctx: HookContext, loop_result: Any) -> None:
    total = int(ctx.state.hook_state.get(_TOOL_COUNT_KEY, 0))
    log.info(
        "━━━ hypothesis loop 结束: status=%s turns=%d tool_calls=%d run=%s ━━━",
        getattr(loop_result, "status", "?"),
        getattr(loop_result, "turns", ctx.turn),
        total,
        ctx.state.run_id,
    )


def _artifact_recovery_on_end(ctx: HookContext, loop_result: Any) -> None:
    recovered = recover_missing_artifacts(ctx.state)
    if recovered:
        log.info(
            "artifact_recovery: 补保存 %d 个 artifact (run=%s)",
            len(recovered),
            ctx.state.run_id,
        )
        ctx.state.append_transcript(
            "artifact_recovery",
            recovered=[{k: v for k, v in r.items() if k in ("artifact_type", "id", "staged_file")} for r in recovered],
        )


async def _auto_validation_on_end(ctx: HookContext, _loop_result: Any) -> None:
    """若模型漏调 validate，在 QC 前补跑，避免「无报告 + treat_missing」与真实质量脱节。

    已有 passed=true 的报告则跳过；缺失或上次失败则重跑，把最新结果交给
    framework mechanical quality_check。
    """
    if ctx.harness.node_type != "hypothesis":
        return

    existing = ctx.state.list_artifacts("hypothesis_output_validation")
    if existing:
        rec = ctx.state.read_artifact(existing[-1]["id"]) or {}
        if (rec.get("metadata") or {}).get("passed") is True:
            return

    # 至少要有一类核心产物，否则空跑无意义
    if not (
        ctx.state.list_artifacts("pre_registration")
        or ctx.state.list_artifacts("research_plan")
    ):
        return

    from nodes.hypothesis.tools.output_validator import _validate_hypothesis_outputs

    result = await _validate_hypothesis_outputs(ctx.state, save_report=True)
    ctx.state.append_transcript(
        "hypothesis_auto_validation",
        passed=bool(result.get("passed")),
        failed_checks=(result.get("failed_checks") or [])[:20],
        artifact_id=result.get("artifact_id"),
        reason="missing_or_failed_validation_before_qc",
    )
    log.info(
        "hypothesis_auto_validation: passed=%s failed=%s (run=%s)",
        result.get("passed"),
        result.get("failed_checks"),
        ctx.state.run_id,
    )


_DEFAULT_REQUIRED_OUTPUTS = (
    "research_state",
    "pre_registration",
    "research_plan",
    "hypothesis_innovation_report",
    "hypothesis_research_overview",
)
_PROGRESS_ARTIFACT_TYPES = frozenset({
    "pre_registration",
    "research_plan",
    "hypothesis_innovation_report",
    "hypothesis_research_overview",
    "hypothesis_conclusion_audit",
    "hypothesis_cluster_report",
    "research_state",
})


def _completion_gate_snapshot(ctx: HookContext) -> dict[str, Any]:
    """机械快照：缺哪些 required、validate 是否已过。"""
    required = list(ctx.harness.required_outputs or _DEFAULT_REQUIRED_OUTPUTS)
    produced = {a["type"] for a in ctx.state.list_artifacts()}
    missing = [t for t in required if t not in produced]

    validation_passed = False
    failed_checks: list[str] = []
    existing = ctx.state.list_artifacts("hypothesis_output_validation")
    if existing:
        rec = ctx.state.read_artifact(existing[-1]["id"]) or {}
        meta = rec.get("metadata") or {}
        validation_passed = meta.get("passed") is True
        raw_failed = meta.get("failed_checks")
        if isinstance(raw_failed, list):
            failed_checks = [str(x) for x in raw_failed if x]

    has_progress = bool(produced & _PROGRESS_ARTIFACT_TYPES)
    return {
        "required": required,
        "missing": missing,
        "validation_passed": validation_passed,
        "failed_checks": failed_checks,
        "has_progress": has_progress,
        "has_validation_report": bool(existing),
    }


def _completion_gate_debt_nudge(ctx: HookContext) -> str | None:
    """中途催促：产物很多但 completion gate 未闭合时，优先补齐 required + validate。

    Study2/3/4 失败模式：烧满轮次做 audit/HIF/修订，却缺 overview / research_plan /
    research_state，或 validate 未过。收尾闸拦不住 max_turns 耗尽；必须在还有轮次时把
    完成清单摆到面前，并明确额外 audit 不得优先于 completion gate。
    """
    snap = _completion_gate_snapshot(ctx)
    if snap["validation_passed"] and not snap["missing"]:
        return None

    max_turns = getattr(ctx.harness, "max_turns", 0) or 0
    remaining = (max_turns - ctx.turn) if max_turns > 0 else None
    late = remaining is not None and remaining <= max(5, max_turns // 4)
    # 太早（尚无实质进度且轮次充裕）不噪音；有进度或进入后半程才催
    if not snap["has_progress"] and not late and ctx.turn < 6:
        return None
    # 刚开局、什么都没有 → 让 workflow 自然走
    if not snap["has_progress"] and not snap["missing"]:
        return None
    if not snap["has_progress"] and ctx.turn < 3:
        return None

    lines = [
        "[hypothesis] **完成闸未闭合**（required outputs + validate 优先于额外 audit）：",
    ]
    if snap["missing"]:
        lines.append(f"  - 缺失 required：{snap['missing']}")
    else:
        lines.append("  - required artifacts 已齐")
    if snap["validation_passed"]:
        lines.append("  - validate_hypothesis_outputs：passed=true")
    elif snap["has_validation_report"]:
        detail = (
            f"（失败项：{', '.join(snap['failed_checks'][:8])}）"
            if snap["failed_checks"] else ""
        )
        lines.append(f"  - validate_hypothesis_outputs：未通过{detail} → 当场修并重跑")
    else:
        lines.append(
            "  - 尚未跑通 validate_hypothesis_outputs → 核心产物就绪后立刻调用，"
            "勿等全部 polish 完"
        )
    lines += [
        "",
        "本轮优先顺序（完成闸 > 锦上添花）：",
        "  1) 补齐缺失 required（research_plan / overview / research_state / HIF / prereg）",
        "  2) `validate_hypothesis_outputs()` → 按 failed_checks 修 → 再 validate 至 passed=true",
        "  3) 之后才继续额外 cluster / conclusion audit / evolve / 反复重审",
        "禁止在 completion gate 未闭合时把剩余轮次花在非必需 audit artifact 上。",
    ]
    if late and remaining is not None:
        lines.append(
            f"**只剩约 {remaining} 轮** —— 再不闭合会以 incomplete 收场"
            "（即使已有大量 audit/HIF 产物）。"
        )
    return "\n".join(lines)


def _missing_outputs_nudge_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """Inject system nudges for truncation / capital_basis / overview / completion gate."""
    notes: list[str] = []

    if ctx.state.hook_state.pop(_CAPITAL_BASIS_NUDGE_KEY, None):
        notes.append(
            "[hypothesis] freeze_artifact 因 metadata.capital_basis 失败。"
            "正确形状（二选一）：\n"
            '  metadata.capital_basis = ["claim_…", …]   # 承重 claim id 列表\n'
            '  metadata.capital_basis = "none_found"     # 检索过但无相关资本\n'
            '禁止 {"item": ["claim_…"]}（tool-call 数组包装会被判「未声明」）。\n'
            "framework 已尝试自动 unwrap；若仍失败请用上述正确形状重 save 后再 freeze。"
            "不要空转多次 save+freeze。"
        )

    if ctx.state.hook_state.pop(_OVERVIEW_AFTER_FREEZE_NUDGE_KEY, None):
        notes.append(
            "[hypothesis] freeze 已多次卡在 capital_basis，且 hypothesis_research_overview "
            "仍缺失。请先 "
            "stage_hypothesis_draft(hypothesis_research_overview) → "
            "save_artifact(artifact_type='hypothesis_research_overview', content_from_file=drafts/hypothesis_research_overview.md)，"
            "再回头用正确 capital_basis 重试 freeze。overview 是 required_output，"
            "不要把全部轮次耗在 freeze 上。"
        )

    if ctx.state.hook_state.get(_TRUNCATED_TURN_KEY) == ctx.turn - 1:
        required = set(ctx.harness.required_outputs or [])
        produced = {a["type"] for a in ctx.state.list_artifacts()}
        missing = required - produced
        if missing:
            notes.append(
                "[hypothesis] 上一轮 LLM 输出被截断，以下必需产出仍缺失："
                f"{sorted(missing)}。"
                " research_plan 若 audit 已通过应已自动保存；"
                " overview 请用 stage_hypothesis_draft → save_artifact(content_from_file=<返回的 staged_file>)，"
                " **禁止 content_b64  inline 大正文**。"
            )

    gate_note = _completion_gate_debt_nudge(ctx)
    if gate_note:
        notes.append(gate_note)

    if not notes:
        return None
    return [LLMMessage(role="system", content="\n\n".join(notes))]


def _rewrite_prereg_metadata(state: Any, artifact_id: str, metadata: dict) -> bool:
    """Re-save an unfrozen pre_registration with normalized metadata. Returns True if rewritten."""
    rec = state.read_artifact(artifact_id) or {}
    if not rec or rec.get("type") != "pre_registration":
        return False
    if (rec.get("metadata") or {}).get("frozen"):
        return False
    try:
        state.save_artifact(
            "pre_registration",
            rec.get("name") or artifact_id,
            rec.get("content") or "",
            metadata=metadata,
        )
    except Exception as exc:
        log.warning("rewrite prereg metadata failed for %s: %s", artifact_id, exc)
        return False
    return True


def _latest_prereg_id(state: Any) -> str | None:
    arts = state.list_artifacts("pre_registration") or []
    if not arts:
        return None
    return arts[-1].get("id")


async def _fix_capital_basis_metadata_on_turn_end(ctx: HookContext) -> None:
    """After save/freeze: unwrap {"item":…} capital_basis and optionally retry freeze.

    e2e6: agent repeatedly saved capital_basis as {"item":[claim_ids]}, freeze gate
    treated it as undeclared, burned all turns, never wrote overview.
    """
    records = ctx.tool_call_records or []
    if not records:
        return

    # 1) Normalize metadata on any pre_registration saved this turn.
    for rec in records:
        if rec.get("name") != "save_artifact":
            continue
        result = rec.get("result") or {}
        if not isinstance(result, dict):
            continue
        args = rec.get("args") or {}
        if args.get("artifact_type") and args.get("artifact_type") != "pre_registration":
            continue
        artifact_id = result.get("id") or result.get("artifact_id")
        if not artifact_id and args.get("name"):
            # Fallback: locate by name among prereg artifacts
            for a in ctx.state.list_artifacts("pre_registration") or []:
                if a.get("name") == args.get("name"):
                    artifact_id = a.get("id")
                    break
        if not artifact_id:
            continue
        stored = ctx.state.read_artifact(artifact_id) or {}
        if stored.get("type") != "pre_registration":
            continue
        meta, changed = normalize_prereg_metadata(stored.get("metadata") or {})
        if not changed:
            continue
        if _rewrite_prereg_metadata(ctx.state, artifact_id, meta):
            ctx.state.append_transcript(
                "hypothesis_capital_basis_normalized",
                artifact_id=artifact_id,
                capital_basis=meta.get("capital_basis"),
            )
            log.info(
                "turn %d: normalized capital_basis on %s → %r",
                ctx.turn,
                artifact_id,
                meta.get("capital_basis"),
            )

    # 2) If freeze failed on capital_basis, fix + retry once.
    freeze_failed_capital = False
    freeze_artifact_id: str | None = None
    for rec in records:
        if rec.get("name") != "freeze_and_register":
            continue
        result = rec.get("result") or {}
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                result = {"status": "error", "error": result}
        if not isinstance(result, dict):
            continue
        err = str(result.get("error") or "")
        if result.get("status") == "error" and "capital_basis" in err:
            freeze_failed_capital = True
            freeze_artifact_id = (rec.get("args") or {}).get("artifact_id")
            break

    if not freeze_failed_capital:
        return

    fails = int(ctx.state.hook_state.get(_FREEZE_CAPITAL_FAILS_KEY, 0)) + 1
    ctx.state.hook_state[_FREEZE_CAPITAL_FAILS_KEY] = fails
    ctx.state.hook_state[_CAPITAL_BASIS_NUDGE_KEY] = True

    artifact_id = freeze_artifact_id or _latest_prereg_id(ctx.state)
    if artifact_id:
        stored = ctx.state.read_artifact(artifact_id) or {}
        meta, changed = normalize_prereg_metadata(stored.get("metadata") or {})
        if changed:
            _rewrite_prereg_metadata(ctx.state, artifact_id, meta)
        # Retry freeze once per turn after unwrap (only if we now have a list / none_found).
        basis = meta.get("capital_basis")
        can_retry = basis == "none_found" or (
            isinstance(basis, list) and bool(basis)
        )
        if can_retry:
            try:
                new_result = await tool_registry.execute(
                    "freeze_and_register",
                    ctx.state,
                    artifact_id=artifact_id,
                )
            except Exception as exc:
                log.warning("freeze retry after capital_basis fix failed: %s", exc)
                new_result = {"status": "error", "error": str(exc)}
            ctx.state.append_transcript(
                "hypothesis_freeze_retry_after_capital_basis_fix",
                artifact_id=artifact_id,
                result_status=(
                    new_result.get("status")
                    if isinstance(new_result, dict)
                    else type(new_result).__name__
                ),
                capital_basis=basis,
            )
            log.info(
                "turn %d: freeze retry after capital_basis fix → %s",
                ctx.turn,
                _tool_result_status(new_result),
            )
            if isinstance(new_result, dict) and new_result.get("status") == "success":
                ctx.state.hook_state[_FREEZE_CAPITAL_FAILS_KEY] = 0
                ctx.state.hook_state.pop(_CAPITAL_BASIS_NUDGE_KEY, None)
                return

    # 3) After repeated freeze stalls, nudge overview before burning more turns.
    produced = {a["type"] for a in ctx.state.list_artifacts()}
    if fails >= 2 and "hypothesis_research_overview" not in produced:
        ctx.state.hook_state[_OVERVIEW_AFTER_FREEZE_NUDGE_KEY] = True


def _json_repair_on_turn_start(ctx: HookContext) -> None:
    fixed = sanitize_message_tool_calls(ctx.messages)
    if fixed:
        log.info("turn %d: 修复 %d 条历史 tool-call arguments", ctx.turn, fixed)


async def _json_repair_on_turn_end(ctx: HookContext) -> None:
    sanitize_message_tool_calls(ctx.messages)
    await _fix_capital_basis_metadata_on_turn_end(ctx)
    records = ctx.tool_call_records or []
    if not records:
        return

    assistant = next(
        (m for m in reversed(ctx.messages) if m.role == "assistant" and m.tool_calls),
        None,
    )
    if not assistant or not assistant.tool_calls:
        return

    call_by_id = {tc["id"]: tc for tc in assistant.tool_calls if tc.get("id")}

    for rec in records:
        result = rec.get("result") or {}
        if not (
            rec.get("name") == "save_hypothesis_artifact"
            and isinstance(result, dict)
            and "JSON 解析失败" in str(result.get("error", ""))
        ):
            continue

        tc = next(
            (
                call_by_id[cid]
                for cid, call in call_by_id.items()
                if (call.get("function") or {}).get("name") == "save_hypothesis_artifact"
            ),
            None,
        )
        if tc is None:
            continue

        raw_args = (tc.get("function") or {}).get("arguments") or "{}"
        args, repaired = parse_tool_arguments(raw_args)
        if not args:
            continue
        if repaired:
            (tc.get("function") or {})["arguments"] = repaired

        try:
            new_result = await tool_registry.execute(
                "save_hypothesis_artifact", ctx.state, **args,
            )
        except Exception as exc:
            log.warning("save_hypothesis_artifact 转义修复后重试失败：%s", exc)
            continue

        rec["args"] = args
        rec["result"] = new_result
        call_id = tc.get("id")
        if call_id:
            for msg in reversed(ctx.messages):
                if msg.role == "tool" and msg.tool_call_id == call_id:
                    msg.content = json.dumps(new_result, ensure_ascii=False, default=str)
                    break

        log.info(
            "turn %d: save_hypothesis_artifact JSON 转义已修复并重试 → %s",
            ctx.turn,
            _tool_result_status(new_result),
        )


register_loop_hook(
    LoopHook(
        name="runtime_trace",
        description="hypothesis 运行时细节日志：turn 序号、LLM 请求的 tool、tool 执行结果。",
        on_turn_start=_runtime_trace_on_turn_start,
        on_llm_response=_runtime_trace_on_llm_response,
        on_turn_end=_runtime_trace_on_turn_end,
        on_end=_runtime_trace_on_end,
    )
)

register_loop_hook(
    LoopHook(
        name="json_tool_args_repair",
        description=(
            "修复 LLM tool-call JSON 中非法转义（如 LaTeX \\|）；"
            "unwrap save_artifact metadata.capital_basis 的 {\"item\":…} 包装并必要时重试 freeze；"
            "必要时重试 save_hypothesis_artifact。"
        ),
        on_turn_start=_json_repair_on_turn_start,
        on_turn_end=_json_repair_on_turn_end,
    )
)

register_loop_hook(
    LoopHook(
        name="artifact_recovery",
        description="loop 结束时从 audit 草稿/transcript 补保存缺失的 research_plan。",
        on_end=_artifact_recovery_on_end,
    )
)

async def _validation_before_finish(ctx) -> list | None:
    """收尾闸：没跑过（或没通过）强制自检就不许收尾，把失败项摊给 agent 修。

    实测（E2E 2026-08-07 真课题）：prompt 里"结束前必须
    validate_hypothesis_outputs() passed=true"写了三遍，agent 全程 56 次工具
    调用**一次没调**，最后一轮还在 create_claim 就收工。on_end hook 补跑校验
    时 loop 已经结束 —— 两项失败（research_plan_complete 的 DAG 说明缺失、
    hypothesis_research_overview 未产出）都是它当场就能补的，却只能眼睁睁
    判 incomplete，再拖累下游 reviewer 白跑 40 轮。
    prompt 里的"必须"不是机制；这里把它变成机制。
    """
    from core.llm import LLMMessage

    state = ctx.state
    # 没有核心产物 → 本来就不是"完成态收尾"，不拦（空跑/早退让原路径处理）
    if not (
        state.list_artifacts("pre_registration")
        or state.list_artifacts("research_plan")
    ):
        return None

    existing = state.list_artifacts("hypothesis_output_validation")
    if existing:
        rec = state.read_artifact(existing[-1]["id"]) or {}
        if (rec.get("metadata") or {}).get("passed") is True:
            return None                      # 自检跑过且通过 → 放行

    from nodes.hypothesis.tools.output_validator import _validate_hypothesis_outputs

    result = await _validate_hypothesis_outputs(state, save_report=True)
    if result.get("passed"):
        return None                          # 现跑一遍就通过了 → 放行

    failed = result.get("failed_checks") or []
    state.append_transcript(
        "hypothesis_finish_gate_blocked",
        failed_checks=failed[:20],
        artifact_id=result.get("artifact_id"),
    )
    lines = [
        "⛔ **收尾被拦下**：`validate_hypothesis_outputs()` 没通过，本节点不得在此状态结束。",
        "",
        "框架刚替你跑了一遍自检，以下检查项失败：",
    ]
    for name in failed[:10]:
        lines.append(f"  - `{name}`")
    lines += [
        "",
        f"完整报告见 artifact `{result.get('artifact_id')}`（用 read_artifact 看每项的具体原因）。",
        "",
        "这些多数是你**当场就能补**的产物/字段缺口。逐项修完后再调一次",
        "`validate_hypothesis_outputs()` 确认 passed=true，然后再收尾。",
        "确实修不了的，如实说明为什么，别硬收。",
    ]
    return [LLMMessage(role="system", content="\n".join(lines))]


register_loop_hook(
    LoopHook(
        name="hypothesis_auto_validation",
        description=(
            "loop 结束时若未通过 validate_hypothesis_outputs，自动补跑自检，"
            "写入 hypothesis_output_validation 供框架 mechanical QC 判定 "
            "（防 required artifacts 齐但审计失败仍标 completed）。"
        ),
        on_end=_auto_validation_on_end,
        on_before_finish=_validation_before_finish,
    )
)

register_loop_hook(
    LoopHook(
        name="truncation_nudge",
        description=(
            "截断 / capital_basis / overview 卡住，以及 completion gate 未闭合"
            "（缺 required 或 validate 未过）时注入 system 催促；"
            "额外 audit 不得优先于完成闸。"
        ),
        on_turn_start=_missing_outputs_nudge_on_turn_start,
    )
)


# ── Analysis 轮次 briefing + research_state 收尾闸（v2.1 P3c）───────────────

_ROUND_BRIEFING_KEY = "_analysis_round_briefing_shown"


def _experiment_evidence(state) -> list[str]:
    """experiment 节点交付的产物 —— 有结果才谈得上"后续轮"。

    扫盘而不是问模型"有没有新实验"：模型答不准，而且这是可机械观察的事实。
    走框架的 artifact 归属（owner_node），不自己拼 `<worktree>/experiment/
    artifacts/*.json` —— 产物目录由框架单点决定，自己拼就是第二个真相源。
    """
    from nodes.hypothesis.tools.analysis_mode import experiment_artifact_ids

    try:
        return sorted(experiment_artifact_ids(state))
    except Exception:
        return []


def _resolve_and_record_mode(ctx) -> dict:
    """机械解析本趟是 plan 还是 revise，并写进 `_request_mode`。

    为什么由节点自己解析而不是等调用方传：这一趟是"第一次出计划"还是"拿实验
    结果回来改计划"，是**项目状态的事实**（盘上有没有冻结 prereg、有没有
    research_state），不是调用方的意图。可机械回答的判断不要交给调用方去猜 ——
    orchestrator 忘了传 mode，节点就又跑一遍最重的流程，而且没人会发现。

    调用方显式传的 `mode` 仍然有效（`plan` 可强制重出计划）；但 `revise`
    不能被"说进存在"，没有生效协议时一律降级回 plan（否则它就成了跳过整套
    预注册的绕行路径）。
    """
    from nodes.hypothesis.tools.analysis_mode import MODES, resolve_mode

    state = ctx.state
    cached = state.hook_state.get("_analysis_mode_resolution")
    if isinstance(cached, dict):
        return cached

    resolution = resolve_mode(state, state.hook_state.get("node_inputs"))
    state.hook_state["_analysis_mode_resolution"] = resolution
    # `_request_mode` 是框架读交付契约 / QC 适用性 / 轮次预算的那一个字段
    # （core.executor / core.quality_checks / core.agent_loop 都读它）。
    # 解析结果必须落在它上面，否则四层分流只贯通了 prompt 那一层。
    if resolution["mode"] in MODES:
        state.hook_state["_request_mode"] = resolution["mode"]
    state.append_transcript(
        "analysis_mode_resolved",
        mode=resolution["mode"],
        declared=resolution.get("declared"),
        downgraded=bool(resolution.get("downgraded")),
        reason=resolution.get("reason"),
    )
    return resolution


_REVISE_PLAYBOOK = (
    "**本轮是 `revise`，不是重出计划。** 不要从零重做假设生成、不要重跑 "
    "cluster / evolve / HIF 打分、不要为了「让门禁看见」而再冻一份 prereg。\n"
    "收尾判据已经按 revise 收窄：假说类判据只在**本轮真的新增了科学承诺**时才审。\n\n"
    "本轮的工作面：\n"
    "1. `read_research_state()` —— 上一轮判到哪、哪些假说还 active。\n"
    "2. 读 experiment 的结果产物（`list_artifacts` / `read_artifact`）。\n"
    "3. **裁决**：每条假说给 supported / refuted / inconclusive，"
    "supported/refuted 必须在 evidence 里填真实的 experiment 产物 id 或 run id"
    "（框架会核对它指不指得到东西）。\n"
    "4. **计划要不要改**：\n"
    "   - 只是改几个数字/换个参数/补一步 → 直接重存 research_plan，不必动 prereg。\n"
    "   - 要新增或改写**命题**（新假设、改 falsifier）→ 那是新承诺：写新一份 "
    "pre_registration 草稿 → 跑审计 → `freeze_artifact`。"
    "已冻结的协议**不可改**，只能加新版本。\n"
    "   - 不用改 → 什么都别动。\n"
    "5. `update_research_state(verdict=..., hypotheses=[...], change_reason=...)` "
    "出新一版。verdict 决定下一步：continue / pivot / ready_candidate / abort。"
)


def _analysis_round_briefing(ctx) -> list | None:
    """开局告诉 Analysis：这是第几轮、什么模式、上一轮判到哪、有哪些新实验证据。

    P3c 的核心之一。节点从"每次都从零想一遍"变成"接着上一版往下走"，
    靠的必须是**框架把状态摆到它面前**，而不是 prompt 让它自己去翻。
    实测教训：契约/状态只要还需要模型主动去查，它就有一半的概率不查。

    每个 state 注入一次（状态在一轮之内不变）。
    """
    from core.llm import LLMMessage
    from nodes.hypothesis.tools.analysis_mode import MODE_REVISE
    from nodes.hypothesis.tools.research_state import current_state, _meta

    state = ctx.state
    resolution = _resolve_and_record_mode(ctx)
    if state.hook_state.get(_ROUND_BRIEFING_KEY):
        return None
    state.hook_state[_ROUND_BRIEFING_KEY] = True

    record = current_state(state)
    evidence = _experiment_evidence(state)
    downgrade_note = (
        ["", f"ℹ️ {resolution['reason']}"] if resolution.get("downgraded") else []
    )

    if record is None or resolution["mode"] != MODE_REVISE:
        prior = int((_meta(record) or {}).get("version") or 0)
        lines = [
            f"🧭 **Analysis 本趟模式：`{resolution['mode']}`（出计划）"
            f"—— 第 {prior + 1} 轮**",
            f"　　{resolution['reason']}",
            "",
            "按出计划的流程走：先写 **`## Research Questions`**（每个问题的 "
            "output_kind、【是命题才写】proposition、以及冻结的闭合条件清单）→ "
            "只给带命题的问题立假设 + 结构化 falsifier → 审计 → prereg 冻结 → "
            "research_plan。",
            "",
            "⚠️ **一等公民是研究问题，假设只是「带命题的问题」。** 产出是一个数 / "
            "一张图或库 / 一个方法 / 一个解释 / 一次复现 / 一个推导时**不要写 "
            "proposition**，只给闭合条件（数值条或陈述条都行），**不要编数值阈值**。",
            "",
            "冻结之后**必须** `update_research_state(verdict=..., hypotheses=[...])` "
            "建立 v1 —— 它是后续每一轮的接续点，也是 writing 前的证据总账。",
        ] + downgrade_note
        if evidence:
            lines += [
                "",
                f"⚠️ 但 experiment 已经交了 {len(evidence)} 个产物，"
                "却没有 research_state。先读它们，别当没发生过。",
            ]
        return [LLMMessage(role="system", content="\n".join(lines))]

    meta = _meta(record)
    version = meta.get("version")
    rows = meta.get("hypotheses") or []
    unresolved = [str(r.get("id")) for r in rows
                  if isinstance(r, dict) and str(r.get("status")) == "active"]
    lines = [
        f"🧭 **Analysis 本趟模式：`revise`（改计划）—— 第 {int(version or 0) + 1} 轮**",
        f"（上一版 research_state = v{version}，verdict=`{meta.get('verdict')}`）",
        "",
        f"- 未裁决假说：{', '.join(unresolved) if unresolved else '（无）'}",
        f"- 上一版 next_steps：{'; '.join(str(x) for x in (meta.get('next_steps') or [])[:5]) or '（无）'}",
        f"- 上一版 gaps：{'; '.join(str(x) for x in (meta.get('gaps') or [])[:5]) or '（无）'}",
        f"- experiment 现有产物：{len(evidence)} 个"
        + (f"（{', '.join(evidence[:6])}{' …' if len(evidence) > 6 else ''}）" if evidence else ""),
        "",
        _REVISE_PLAYBOOK,
    ]
    return [LLMMessage(role="system", content="\n".join(lines))]


async def _research_state_before_finish(ctx) -> list | None:
    """收尾闸：本轮产生了新科学内容，就不许不更新 research_state 而收尾。

    与 validate_hypothesis_outputs 那道闸同构 —— prompt 写"必须更新"没用，
    要在它想收尾的那一刻拦下来。触发条件机械判定：
      - 有 frozen prereg（说明这轮确实产出了协议），且
      - 当前 research_state 版本号没有比进入本轮时更高
    """
    from core.llm import LLMMessage
    from nodes.hypothesis.tools.analysis_mode import has_frozen_prereg
    from nodes.hypothesis.tools.research_state import (
        current_state, _meta, frozen_prereg_hypothesis_ids,
    )

    state = ctx.state
    frozen_ids = frozen_prereg_hypothesis_ids(state)
    # 触发条件是"**协议冻结了**"，不是"协议里有假设"。非命题裁决型研究的
    # prereg 没有 Hx 段，frozen_ids 是空的 —— 按旧判据它永远不欠 research_state，
    # 于是整条裁决账在这类研究上直接不存在。放宽假设要求的同时必须补上这一刀，
    # 否则"不要求假设"就顺带把"要有研究状态"也免掉了。
    if not frozen_ids and not has_frozen_prereg(state):
        return None                       # 还没冻结协议 → 不是"有科学产出的收尾"

    baseline = state.hook_state.get("_research_state_baseline_version")
    if baseline is None:
        baseline = 0
    meta = _meta(current_state(state))
    version = int(meta.get("version") or 0)
    if version > int(baseline):
        return None                       # 本轮已经出过新版本 → 放行

    state.append_transcript(
        "research_state_finish_gate_blocked",
        baseline_version=baseline,
        current_version=version,
        frozen_hypotheses=sorted(frozen_ids),
    )
    return [LLMMessage(role="system", content="\n".join([
        "⛔ **收尾被拦下**：本项目已有冻结的 pre_registration，本轮却没有产出"
        "新一版 research_state。",
        "",
        f"已冻结的假说：{', '.join(sorted(frozen_ids)) or '（无 —— 本研究非命题裁决型）'}",
        f"当前 research_state 版本：{'v' + str(version) if version else '（不存在）'}",
        "",
        "research_state 是研究状态的唯一接续点 —— 没有它，下一轮 Analysis 无从知道",
        "上一轮判到哪、哪些假说还没裁决，writing 也拿不到证据总账。",
        "",
        "现在调 `update_research_state(verdict=..., hypotheses=[...])`：",
        "  - 每条假说给 id + status（本轮刚冻结、还没做实验 → `active`）",
        "  - 已被实验裁决的给 `supported`/`refuted` 并在 evidence 里填实验产物 id",
        "  - 有上一版时必须写 change_reason",
        "完成后再收尾。",
    ]))]


def _research_state_snapshot_baseline(ctx) -> None:
    """进入本轮时记下版本基线 —— 收尾闸靠它判断"本轮有没有出新版本"。"""
    state = ctx.state
    if "_research_state_baseline_version" in state.hook_state:
        return
    from nodes.hypothesis.tools.research_state import current_state, _meta

    state.hook_state["_research_state_baseline_version"] = int(
        (_meta(current_state(state)) or {}).get("version") or 0
    )


def _outstanding_research_state_debt(ctx) -> list | None:
    """协议冻结之后还欠着 research_state —— 在**还有轮数可用时**就说。

    E2E v16 实测：hypothesis 烧满 40 轮（max_turns）结束，`on_before_finish`
    **一次都没被调用**（0 次拦截记录），research_state 从头到尾没产出，最后
    由 QC 事后判 incomplete，reviewer 拒绝出具意见，决策包连 PROCEED 都不给。

    收尾闸只守"自愿收尾"这一条路 —— **耗尽轮数就是它的绕过路径，而且不吭声**。
    没轮数的时候再拦也拦不住什么：得在还来得及做的时候把这笔欠账摆到面前。

    只在协议已冻结（确实产生了要记账的科学内容）且本轮还没出新版本时提醒；
    出过就闭嘴，不制造噪音。
    """
    from core.llm import LLMMessage
    from nodes.hypothesis.tools.analysis_mode import has_frozen_prereg
    from nodes.hypothesis.tools.research_state import (
        current_state, _meta, frozen_prereg_hypothesis_ids,
    )

    state = ctx.state
    try:
        frozen_ids = frozen_prereg_hypothesis_ids(state)
        frozen_any = bool(frozen_ids) or has_frozen_prereg(state)
    except Exception:
        return None
    if not frozen_any:
        return None

    baseline = int(state.hook_state.get("_research_state_baseline_version") or 0)
    try:
        version = int((_meta(current_state(state)) or {}).get("version") or 0)
    except Exception:
        version = 0
    if version > baseline:
        return None

    max_turns = getattr(ctx.harness, "max_turns", 0) or 0
    remaining = max_turns - ctx.turn if max_turns > 0 else None
    urgency = ""
    if remaining is not None and remaining <= max(3, max_turns // 5):
        urgency = f"**本轮只剩约 {remaining} 轮**，再不做就会以 incomplete 收场（"\
                  "reviewer 会拒绝出具意见，整条流程停在决策点）。"

    subject = (
        f"（{', '.join(sorted(frozen_ids))}）" if frozen_ids
        else "（本研究非命题裁决型，协议里没有 Hx；research_state 记的是研究问题的进展）"
    )
    return [LLMMessage(role="system", content=(
        f"[hypothesis] 未结清的本轮义务：协议已冻结{subject}，"
        f"但 research_state 仍停在 v{version}。\n"
        f"请调 `update_research_state(verdict=..., hypotheses=[...])` 建立/更新版本，"
        f"把每条冻结假说 / 研究问题登记为待裁决状态。它是研究状态的唯一接续点 —— "
        f"没有它，下一轮 Analysis 不知道判到哪，writing 也拿不到证据总账。{urgency}"
    ))]


def _analysis_round_on_turn_start(ctx) -> list | None:
    _research_state_snapshot_baseline(ctx)
    messages = _analysis_round_briefing(ctx) or []
    debt = _outstanding_research_state_debt(ctx)
    if debt:
        messages = list(messages) + list(debt)
    return messages or None


register_loop_hook(
    LoopHook(
        name="analysis_round_state",
        description=(
            "开局注入轮次 briefing（第几轮 / 上一版 verdict / 未裁决假说 / "
            "experiment 新产物），并在收尾时强制产出新一版 research_state。"
        ),
        on_turn_start=_analysis_round_on_turn_start,
        on_before_finish=_research_state_before_finish,
    )
)
