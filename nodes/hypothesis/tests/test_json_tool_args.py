"""JSON tool-call 转义修复 + capital_basis metadata 归一化测试。"""
from __future__ import annotations

import json

from core.llm import LLMMessage
from nodes.hypothesis.json_tool_args import (
    normalize_prereg_metadata,
    parse_tool_arguments,
    repair_json_string_escapes,
    sanitize_message_tool_calls,
    unwrap_item_envelope,
)


def test_repair_invalid_pipe_escape() -> None:
    raw = '{"content": "弛豫后 |F\\|<0.01 eV/Å"}'
    repaired = repair_json_string_escapes(raw)
    parsed = json.loads(repaired)
    assert parsed["content"] == "弛豫后 |F\\|<0.01 eV/Å"


def test_parse_tool_arguments_recovers_latex_content() -> None:
    raw = (
        '{"artifact_type": "research_plan", "name": "tC40_Research_Plan", '
        '"content": "# Plan\\n|F\\|<0.01"}'
    )
    args, repaired = parse_tool_arguments(raw)
    assert repaired is not None
    assert args["artifact_type"] == "research_plan"
    assert "|F\\|" in args["content"]


def test_sanitize_message_tool_calls_fixes_assistant_history() -> None:
    bad_args = (
        '{"artifact_type": "research_plan", "name": "x", '
        '"content": "gate: |F\\|<0.01"}'
    )
    msg = LLMMessage(
        role="assistant",
        content=None,
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": "save_hypothesis_artifact", "arguments": bad_args},
        }],
    )
    fixed = sanitize_message_tool_calls([msg])
    assert fixed == 1
    json.loads(msg.tool_calls[0]["function"]["arguments"])


def test_unwrap_singleton_envelope_variants() -> None:
    assert unwrap_item_envelope({"item": ["a", "b"]}) == ["a", "b"]
    assert unwrap_item_envelope({"item": {"item": ["a"]}}) == ["a"]
    assert unwrap_item_envelope({"items": ["a"]}) == ["a"]
    assert unwrap_item_envelope({"value": {"data": ["a", "b"]}}) == ["a", "b"]
    assert unwrap_item_envelope({"elements": "none_found"}) == "none_found"
    assert unwrap_item_envelope(["a"]) == ["a"]
    assert unwrap_item_envelope("none_found") == "none_found"
    # Real multi-key objects must not be peeled at the top level.
    obj = {"substitutes": {"gpu": "local"}, "deferred": ["annotators"]}
    assert unwrap_item_envelope(obj) == obj


def test_normalize_prereg_metadata_capital_basis() -> None:
    meta, changed = normalize_prereg_metadata({
        "capital_basis": {"items": {"value": ["claim_aaa", "claim_bbb"]}},
        "execution_commitment": {"substitutes": "", "deferred": ""},
    })
    assert changed is True
    assert meta["capital_basis"] == ["claim_aaa", "claim_bbb"]
    assert meta["execution_commitment"]["substitutes"] == {}
    assert meta["execution_commitment"]["deferred"] == []


def test_sanitize_unwraps_save_artifact_capital_basis_in_history() -> None:
    args = {
        "artifact_type": "pre_registration",
        "name": "Si_Prereg",
        "content": "# draft",
        "metadata": {
            "capital_basis": {"item": ["claim_87ee4b423406"]},
        },
    }
    msg = LLMMessage(
        role="assistant",
        content=None,
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "save_artifact",
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        }],
    )
    fixed = sanitize_message_tool_calls([msg])
    assert fixed == 1
    parsed = json.loads(msg.tool_calls[0]["function"]["arguments"])
    assert parsed["metadata"]["capital_basis"] == ["claim_87ee4b423406"]
