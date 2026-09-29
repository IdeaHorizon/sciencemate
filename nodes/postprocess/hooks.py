"""Postprocess-specific Agent Loop reliability hooks."""

from __future__ import annotations

import json
import re
from typing import Any

from core.llm import LLMMessage
from core.loop_hooks import HookContext, LoopHook, register_loop_hook

_TERMINAL_ERROR = re.compile(
    r"incompatible units|not present in the caller's visual_requests|"
    r"differs from the caller contract|requires upstream metadata",
    re.IGNORECASE,
)
_COUNT_KEY = "_postprocess_terminal_error_count"
_COMPLETION_TOOLS = {"render_figure"}


def _error_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    error = result.get("error")
    if isinstance(error, str):
        return error
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except TypeError:
        return str(result)


def _request_count(state: Any) -> int:
    node_inputs = state.hook_state.get("node_inputs")
    if not isinstance(node_inputs, dict):
        return 0
    requests = node_inputs.get("visual_requests")
    if requests is None:
        requests = node_inputs.get("figure_requests")
    if isinstance(requests, dict):
        return 1
    if isinstance(requests, list):
        return sum(isinstance(item, dict) for item in requests)
    return 0


def _successful_figure_final_text(result: dict[str, Any]) -> str:
    files = result.get("files") or []
    paths = [str(item.get("absolute_path") or item.get("path")) for item in files]
    path_lines = "\n".join(f"- {path}" for path in paths if path and path != "None")
    return (
        "Scientific figure rendered in the sandbox with mechanically recorded "
        f"provenance. Figure record: {result.get('figure_id')}.\n"
        "Source, render-code, and output hashes are bound; the mechanical audit "
        "ran with no findings."
        + (f"\nDeliverables:\n{path_lines}" if path_lines else "")
    )


def _postprocess_terminal_error_on_turn_end(
    ctx: HookContext,
) -> LLMMessage | None:
    terminal = []
    upstream_rework_registered = False
    for record in ctx.tool_call_records:
        name = str(record.get("name") or "")
        result = record.get("result")
        # 判决拆除 B 刀：提前收工只看机械事实 —— 唯一请求的 figure 记录铸出
        # 来了、findings 为空。有 findings 时把回合留给 agent 自己决定修不修。
        if (
            name in _COMPLETION_TOOLS
            and isinstance(result, dict)
            and result.get("status") == "success"
            and result.get("figure_id")
            and not result.get("findings")
            and _request_count(ctx.state) == 1
        ):
            reason = "single visualization request completed with a clean figure record"
            ctx.state.hook_state["_loop_terminal"] = {
                "reason": reason,
                "requested_by": "postprocess_success_finalizer",
                "status": "completed",
                "final_text": _successful_figure_final_text(result),
            }
            ctx.state.append_transcript(
                "postprocess_success_terminal",
                turn=ctx.turn,
                tool=name,
                figure_id=result.get("figure_id"),
                reason=reason,
            )
            return None
        text = _error_text(result)
        if (
            isinstance(result, dict)
            and result.get("status") == "error"
            and _TERMINAL_ERROR.search(text)
        ):
            terminal.append({"tool": name, "error": text[:500]})
        if (
            name == "request_upstream_rework"
            and isinstance(result, dict)
            and result.get("status") == "success"
        ):
            upstream_rework_registered = True

    if not terminal and not upstream_rework_registered:
        return None

    count = int(ctx.state.hook_state.get(_COUNT_KEY) or 0) + 1
    ctx.state.hook_state[_COUNT_KEY] = count
    ctx.state.append_transcript(
        "postprocess_terminal_condition_observed",
        turn=ctx.turn,
        count=count,
        terminal_errors=terminal,
        upstream_rework_registered=upstream_rework_registered,
    )
    if count >= 2:
        reason = (
            "postprocess terminal capability/contract condition repeated after an explicit "
            "stop instruction; substitutes and further retries are prohibited"
        )
        ctx.state.hook_state["_loop_terminal"] = {
            "reason": reason,
            "requested_by": "postprocess_terminal_error_breaker",
            "status": "completed",
            "final_text": (
                "Visualization request is incomplete: the required scientific "
                "contract is unavailable. No substitute figure or downgraded request was "
                "created. See the recorded tool error/upstream rework request for the exact "
                "required capability."
            ),
        }
        ctx.state.append_transcript(
            "postprocess_terminal_circuit_break",
            turn=ctx.turn,
            count=count,
            reason=reason,
        )
        return None

    detail = terminal[0]["error"] if terminal else "upstream rework has been registered"
    return LLMMessage(
        role="system",
        content=(
            "⛔ TERMINAL POSTPROCESS CONDITION. The last tool result is an authoritative "
            f"capability/request-contract boundary: {detail}\n"
            "Do not retry the same rendering, change request_id/intent/asset_kind, create a "
            "placeholder, or substitute another chart. Do not call another tool. Return one "
            "concise final response that states the request is incomplete, preserves the "
            "reason, and names the required capability or upstream rework."
        ),
    )


register_loop_hook(
    LoopHook(
        name="postprocess_terminal_error_breaker",
        description=(
            "Stops repeated retries after an authoritative visualization capability, "
            "scientific-contract, or upstream-rework terminal result."
        ),
        on_turn_end=_postprocess_terminal_error_on_turn_end,
    )
)


__all__ = ["_postprocess_terminal_error_on_turn_end"]


# ── 入站请求 id briefing（v2.1 P5）────────────────────────────────────────

_REQUEST_BRIEFING_KEY = "_visual_request_ids_shown"


def _visual_request_briefing(ctx) -> list | None:
    """开局把**本次要处理的 request_id** 摆到节点面前。

    render_figure 用 `request_id` 把记录对回调用方的请求，而这个 id 是
    `request.get("request_id") or slug(intent)` —— 调用方没显式传时由 intent
    slug 而来，是一个节点**在输入里看不到的字符串**。

    P5 实测：节点连猜 8 次（'visual_requests[0]' / '0' / 'request_0' / '' /
    '[0]' / 'lj_cooling_comparison' …）全部对不上，40 轮烧完。它不是笨 ——
    要求别人引用一个它无从枚举的标识符，本来就只能靠猜。

    id 是每次调用才产生的运行时事实，写不进静态契约 —— 修在开局注入。
    """
    from core.llm import LLMMessage

    state = ctx.state
    if state.hook_state.get(_REQUEST_BRIEFING_KEY):
        return None

    node_inputs = state.hook_state.get("node_inputs")
    if not isinstance(node_inputs, dict):
        return None
    raw = node_inputs.get("visual_requests")
    if raw is None:
        raw = node_inputs.get("figure_requests")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return None

    from nodes.postprocess.contracts import caller_policy, normalize_request
    from nodes.postprocess.tools.figure import RENDER_ATTEMPTS_PER_REQUEST

    rows: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            normalized = normalize_request(item)
        except Exception:
            continue
        try:
            policy = caller_policy(item)
        except Exception:  # noqa: BLE001 —— 政策写坏了由 declare 那边报，这里只列
            policy = {}
        bound = ", ".join(
            f"{key}={value}"
            for key, value in (
                ("asset_kind", policy.get("asset_kind") or normalized["asset_kind"]),
                ("width", policy.get("medium") or f"by purpose={normalized['purpose']}"),
                ("text_language", policy.get("text_language")),
                ("min_font_pt", policy.get("min_font_pt")),
                ("forbid_text", policy.get("forbid_text") or None),
            )
            if value
        )
        rows.append(
            f"  - `{normalized['request_id']}` — {normalized['normalized_intent'][:120]}"
            f"（{bound}）"
        )
    if not rows:
        return None

    state.hook_state[_REQUEST_BRIEFING_KEY] = True
    return [LLMMessage(role="system", content="\n".join([
        "🎯 **本次调用的 visual request（render_figure 要引用的 request_id）**：",
        *rows,
        "",
        "每条请求后面括号里的是**调用方绑定的政策**：asset_kind 决定家族（schematic 只能"
        "按合同编译、不收 code；改标别的家族会被拒），width 决定版心（印刷字号下限 7pt），"
        "text_language=en 时图内任何中日韩文字都拒绝。这些在 declare_figure_contract 与 "
        "render_figure 里机械核，你改不掉 —— 做不到就如实告诉调用方。",
        f"同一个 request_id 最多渲染 {RENDER_ATTEMPTS_PER_REQUEST} 次：先用 execute_python "
        "试画自查，定稿再 render_figure。",
        "流程：读数据（read_artifact）→ figure_contract_schema 取该家族的 schema 与例子 → "
        "declare_figure_contract → render_figure(request_id=..., contract_id=..., "
        "source_artifact_ids=..., output_files=...) 铸记录。request_id 逐字用上面的 —— "
        "它由调用方决定，不要自己编。",
    ]))]


register_loop_hook(
    LoopHook(
        name="visual_request_briefing",
        description="开局列出本次 visual_requests 的 request_id 与 intent，避免节点靠猜。",
        on_turn_start=_visual_request_briefing,
    )
)
