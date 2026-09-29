"""Resolve the project-synthesis contract from a reviewer critique record.

判决拆除·第三波（run_node.py:1690/1725 + writing_gate.py 整套退场）：

writing-gate 曾经是一道派发闸——「没有 project_synthesis / verdict≠ready_to_write
就不能起 writing」，fire_data 里 28 次开火、单 run 连撞 24 次的骚扰墙；配套的
``present_writing_gate_override`` 工具与 pause 类型 ``writing_gate_override`` 是
它的授权环。这些都删了：起 writing 不再被拦，「从未做过综合评估 / verdict 仍是
iterate」由 writing 节点的 input_audit（``scientific_basis.project_synthesis``）
如实入账、进稿件局限节、referee 终审——证据可持久化，判决不可以。

留下的只有这一个纯函数：读一份 review_critique，把 project_synthesis 字段从
metadata（权威）/ legacy content 里解析出来。调用方：writing 的 input_audit 与
chat.py 的 continuous 动作清单。
"""
from __future__ import annotations

import json

_PROJECT_VERDICTS = frozenset({"ready_to_write", "iterate", "pivot", "abort"})


def _content_object(artifact: dict) -> dict:
    """Return a strict JSON object from a critique body, or an empty mapping."""
    raw = artifact.get("content")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def resolve_project_synthesis(artifact: dict) -> dict | None:
    """Resolve gate fields from metadata, with a strict legacy-content fallback.

    New reviewer artifacts stamp the project-synthesis contract into metadata.
    Older typed artifacts could not: ``compose_review_critique`` discarded those
    arguments even when the reviewer emitted them in its JSON body.  Accepting
    recognized JSON fields lets already-completed reviews cross the repaired
    gate without weakening it to free-text inference.

    Metadata remains authoritative.  A conflicting body verdict is surfaced as
    an unresolved verdict so consumers fail closed instead of choosing the more
    permissive value.
    """
    if not isinstance(artifact, dict):
        return None
    metadata = artifact.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    content = _content_object(artifact)

    metadata_scope = metadata.get("scope")
    content_scope = content.get("scope")
    source_node_type = metadata.get("source_node_type") or content.get("source_node_type")
    if metadata_scope not in (None, "project_synthesis"):
        return None
    if content_scope not in (None, "project_synthesis"):
        return None
    if (
        metadata_scope != "project_synthesis"
        and content_scope != "project_synthesis"
        and source_node_type != "_project"
    ):
        return None

    resolved = dict(metadata)
    resolved["scope"] = "project_synthesis"
    for key in (
        "mode",
        "project_verdict",
        "user_requirement_summary",
        "current_state_summary",
        "actionable_next_steps",
        "scores",
        "recommended_action",
        "recommended_target_node",
    ):
        if resolved.get(key) is None and content.get(key) is not None:
            resolved[key] = content[key]
    if resolved.get("scores") is None and content.get("per_dimension_scores") is not None:
        resolved["scores"] = content["per_dimension_scores"]
    if resolved.get("recommended_target_node") is None:
        recommended = resolved.get("recommended_action")
        if isinstance(recommended, dict):
            resolved["recommended_target_node"] = recommended.get("target_node")

    metadata_verdict = metadata.get("project_verdict")
    content_verdict = content.get("project_verdict")
    if metadata.get("review_incomplete") or content.get("review_incomplete"):
        resolved["project_verdict"] = None
        resolved["resolution_error"] = "project_synthesis review is incomplete"
    elif (
        metadata_verdict is not None
        and content_verdict is not None
        and metadata_verdict != content_verdict
    ):
        resolved["project_verdict"] = None
        resolved["resolution_error"] = (
            "metadata.project_verdict conflicts with content.project_verdict"
        )
    elif resolved.get("project_verdict") not in _PROJECT_VERDICTS:
        resolved["project_verdict"] = None
        resolved["resolution_error"] = "project_verdict missing or invalid"

    return resolved
