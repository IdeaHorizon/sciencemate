"""read_producer_transcript —— _reviewer 节点的按需深查工具。

reviewer 默认不看 producer 的 transcript（黑盒 review）。但当 artifact 哪儿可疑
时，可以主动调本工具拉 producer 的完整 transcript 看它的 reasoning 链。

Sibling-run path resolution: 所有子 run 共享同一 base_dir (state.root.parent)，
所以 producer 的 transcript 在 `state.root.parent / producer_run_id / transcript.jsonl`。
"""
from __future__ import annotations

import json
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool


async def _read_producer_transcript(
    state: State,
    producer_run_id: str,
    event_filter: list[str] | str | None = None,
    limit: int = 200,
    **_: Any,
) -> dict:
    """读 producer 子 run 的 transcript。

    event_filter:
      - None 或 'all': 返所有 event
      - list of str (e.g. ['tool_call_start', 'llm_call_end']): 仅返这些 event type
    limit: 最多返多少条 event（保护 context）。
    """
    # producer_run_id 的必填与非空由 parameters_schema 声明，派发口核一次。
    producer_path = state.root.parent / producer_run_id / "transcript.jsonl"
    if not producer_path.exists():
        return {
            "status": "error",
            "error": f"transcript 不存在: {producer_path}",
            "hint": "确认 producer_run_id 正确，且跟本 run 是 sibling（共享 base_dir）",
        }

    events: list[dict] = []
    for line in producer_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(e)

    total = len(events)

    # 过滤
    if event_filter and event_filter != "all":
        if isinstance(event_filter, str):
            event_filter = [event_filter]
        events = [e for e in events if e.get("event") in event_filter]
    n_filtered = len(events)

    # 截断
    truncated = False
    if len(events) > limit:
        events = events[-limit:]   # 取最近的 N 条
        truncated = True

    return {
        "status": "success",
        "producer_run_id": producer_run_id,
        "n_events_total": total,
        "n_events_filtered": n_filtered,
        "n_events_returned": len(events),
        "truncated": truncated,
        "events": events,
    }


register_tool(
    ToolDefinition(
        name="read_producer_transcript",
        # 纯读：结果可用同样参数重调取回。重复调用紧凑化与压缩器都扫这个
        # 声明（core/tool_call_cache.cacheable_tools），不再各写一份名单。
        replayable_read=True,
        description=(
            "读 producer 子 run 的 transcript（按需深查工具）。\n\n"
            "**Use when**（_reviewer 节点）：\n"
            "  - 看完 artifact 觉得 producer 的某个判断不合理 → 拉 transcript 看 reasoning\n"
            "  - 想确认 producer 是否调过某个 tool / 是否被某个 tool 错误干扰\n"
            "  - debug producer 的 LLM 决策链\n\n"
            "**Do NOT use when**：\n"
            "  - 默认审稿（黑盒 review 通常更接近真实 peer review）\n"
            "  - 想看 artifact 内容 → 用 read_artifact / read_external_artifact\n\n"
            "**事件类型常用值**: llm_call_start / llm_call_end / tool_call_start / "
            "tool_call_end\n\n"
            "**参数**：\n"
            "  - producer_run_id：producer 子 run 的 id（从 decision package 拿）\n"
            "  - event_filter：None=所有；'all'=所有；list=指定 event types\n"
            "  - limit：最多返多少条（默认 200，超出取最近的）"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "producer_run_id": {"type": "string", "minLength": 1,
                                       "description": "producer 子 run 的 id"},
                "event_filter": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "description": "None / 'all' / list of event types",
                },
                "limit": {"type": "integer", "default": 200,
                            "minimum": 1, "maximum": 5000},
            },
            "required": ["producer_run_id"],
        },
        risk_level="low",
    ),
    _read_producer_transcript,
)
