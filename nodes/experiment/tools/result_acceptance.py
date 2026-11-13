"""Existing-results acceptance is a task constraint, not a filesystem permission.

The caller can freeze this intent at dispatch. Older callers supplied an explicit
no-regeneration quote in Core's inherited read-only contract. Never infer it from
a filename, a method label, a generic read-only promise, or an execution kwarg.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from typing import Any

from core.side_effect_contract import promised_read_only
from core.tool_registry import ToolDefinition, register_tool

from . import obligations

MODE = "existing_results_only"


def restricted(state: Any) -> bool:
    promise = promised_read_only(state)
    # Narrow legacy bridge: an affirmative restriction on existing results
    # AND an explicit prohibition on re-generation in the same user quote.
    if promise is not None and promise.said_by == "user" and re.search(
        r"(?:^|[。；;，,\s])只(?:检查|核验|验收)已有结果[，,、；;\s]+(?:不得|不要|不)(?:重新生成|重跑)",
        promise.quote,
    ):
        return True
    if not getattr(state, "root", None):
        return False
    path = obligations.obligations_path(state)
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(source).get("acceptance_mode") == MODE
    except FileNotFoundError:
        from core.context_engine import read_first_turn_node_inputs_receipt
        try:
            receipt = read_first_turn_node_inputs_receipt(state)
        except FileNotFoundError:
            pass  # Before first turn: use the dispatch input, never tool arguments.
        else:
            return (receipt.get("values") or {}).get("acceptance_mode") == MODE
        return obligations._node_inputs(state).get("acceptance_mode") == MODE


def refusal(tool: str) -> dict:
    return {"status": "error", "reason": "existing_results_only", "executed": False, "tool": tool,
            "error": "本轮只验收已有结果。请用 read_file 查看数据或旧程序，用 inspect_result_table 复核表格，"
                     "用 safe_write_file 写独立报告；不要启动附件、它的副本或作业。需要重新生成时请另行派发。"}


def is_inspection_command(state: Any, cmd: str) -> bool:
    # Only a single static reader. Full shell syntax remains available outside
    # this mode. Reuse the normal executable-trust check, not basename alone.
    import shlex
    from .safe_bash import _is_read_only_shell_command
    if not isinstance(cmd, str) or not cmd.strip() or any(ch in cmd for ch in "\r\n;`$|&<>(){}\\"):
        return False
    try:
        args = shlex.split(cmd)
    except ValueError:
        return False
    return bool(args and args[0] in {"cat", "wc", "sha256sum", "head", "tail"}
                and _is_read_only_shell_command(cmd, state))


async def inspect_result_table(state: Any, path: str, value: str, group_by: str | None = None,
                               where: dict[str, str] | None = None, record_id: str | None = None, **_: Any) -> dict:
    """Describe rows from one complete, bounded Core file read. No code from the input runs."""
    from shared.tools.builtin import _read_file
    read = await _read_file(state, path)
    if read.get("status") != "success":
        return read
    if read.get("truncated") or read.get("partial_line"):
        return {"status": "error", "error": "Table exceeds one complete bounded read; no aggregate computed."}
    try:
        lines = [line.split("\t", 1)[1] for line in read["content"].split("\n") if line]
        reader = csv.DictReader(io.StringIO("\n".join(lines)), strict=True)
        fields = reader.fieldnames or []
        where = where or {}
        if len(fields) != len(set(fields)) or not {value, *where, *filter(None, (group_by, record_id))} <= set(fields):
            raise ValueError("missing or duplicate CSV column")
        groups: dict[str, list[float]] = {}
        ids: dict[str, list[str]] = {}
        total = 0
        for row in reader:
            total += 1
            if None in row or None in row.values():
                raise ValueError(f"malformed CSV row {reader.line_num}")
            if any(row[k] != v for k, v in where.items()):
                continue
            group = row[group_by] if group_by else ""
            number = float(row[value])
            if not math.isfinite(number) or (group_by and not group) or (record_id and not row[record_id]):
                raise ValueError(f"missing group/id or nonfinite value at row {reader.line_num}")
            groups.setdefault(group, []).append(number)
            if record_id:
                ids.setdefault(group, []).append(row[record_id])
        if not groups:
            raise ValueError("no matching records")
        summary = {k: {"count": len(v), "mean": math.fsum(v) / len(v), "min": min(v), "max": max(v),
                       **({"ids": ids[k], "duplicate_ids": len(set(ids[k])) != len(ids[k])} if record_id else {})}
                   for k, v in groups.items()}
        return {"status": "success", "path": read["path"], "rows_read": total, "where": where,
                "value_column": value, "groups": summary}
    except (ValueError, IndexError, csv.Error, OverflowError) as exc:
        return {"status": "error", "error": str(exc)}


register_tool(ToolDefinition(
    name="inspect_result_table", description="从已有的小型 CSV 完整读一次。可按列筛选、分组、检查记录 ID 重复，"
    "计算计数、均值和范围。它不启动输入程序。较大或非 CSV 输入用 read_file 分页核对。",
    parameters_schema={"type": "object", "properties": {
        "path": {"type": "string"}, "value": {"type": "string"}, "group_by": {"type": "string"},
        "where": {"type": "object", "additionalProperties": {"type": "string"}},
        "record_id": {"type": "string"}}, "required": ["path", "value"], "additionalProperties": False},
), inspect_result_table)
