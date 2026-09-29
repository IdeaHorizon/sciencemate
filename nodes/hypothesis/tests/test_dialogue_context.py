"""dialogue_context：用户 prompt + 父会话 grounding。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from core.state import State
from nodes.hypothesis.tools.dialogue_context import (
    build_dialogue_context,
    extract_constraints_from_text,
    extract_user_prompt_from_inputs,
)


def test_extract_user_prompt_prefers_explicit_keys() -> None:
    assert extract_user_prompt_from_inputs({
        "prompt": "do A",
        "research_question": "do B",
    }) == "do A"


def test_extract_constraints_forbid_and_only_use() -> None:
    text = "工作流只使用 data_import 和 paired_bootstrap；禁止使用或提及 phonon、掺杂"
    cons = extract_constraints_from_text(text)
    assert any("禁止" in c for c in cons)
    assert any("只使用" in c or "只用" in c for c in cons)


def test_build_dialogue_context_from_parent_conversation() -> None:
    base = Path(tempfile.mkdtemp())
    parent_id = "orchestrator__p1"
    (base / parent_id).mkdir()
    (base / parent_id / "conversation.json").write_text(
        json.dumps({
            "messages": [
                {"role": "system", "content": "ignored"},
                {"role": "user", "content": "先做 literature"},
                {"role": "assistant", "content": "已启动"},
                {"role": "user", "content": "改做 CSV 性能；禁止提及 AIMD"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    state = State.new(node_type="hypothesis", base_dir=base, project_id="p1")
    state.parent_run_id = parent_id
    ctx = build_dialogue_context(state, {})
    assert ctx["user_prompt"] and "CSV" in ctx["user_prompt"]
    assert ctx["user_prompt_source"] == "conversation"
    assert any("AIMD" in c for c in ctx["inferred_constraints"])
    assert ctx["n_conversation_messages"] == 4


def test_build_dialogue_context_extracts_must_cover() -> None:
    state = State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))
    ctx = build_dialogue_context(state, {
        "user_prompt": (
            "Agent 原生计算架构研究，包含任务拆分、动态路由、多模型执行和成本分析"
        ),
    })
    themes = ctx["must_cover_themes"]
    assert "任务拆分" in themes
    assert "动态路由" in themes
    assert "多模型执行" in themes
    assert "成本分析" in themes
    assert any("退化" in c for c in ctx["grounding_checklist"])
    assert any("完成闸优先" in c for c in ctx["grounding_checklist"])
    assert any("validate_hypothesis_outputs" in c for c in ctx["grounding_checklist"])


def test_format_dialogue_brief_leads_with_completion_gate() -> None:
    from nodes.hypothesis.tools.dialogue_context import format_dialogue_brief

    state = State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))
    ctx = build_dialogue_context(state, {"user_prompt": "只做一件事"})
    brief = format_dialogue_brief(ctx)
    assert "completion_gate" in brief
    assert "validate_hypothesis_outputs" in brief
    # user_prompt comes first; completion_gate section follows immediately after
    up_idx = brief.index("user_prompt")
    gate_idx = brief.index("completion_gate")
    assert gate_idx > up_idx
    assert "完成闸" in brief or "validate_hypothesis_outputs" in brief