"""capital_basis metadata 自动修复 hook 测试。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from core.harness import NodeHarness
from core.loop_hooks import HookContext
from core.state import State
import nodes.hypothesis.hooks as hyp_hooks


def _make_ctx(tmp_path) -> HookContext:
    state = State.new(node_type="hypothesis", base_dir=tmp_path)
    harness = NodeHarness(
        node_type="hypothesis",
        max_turns=40,
        required_outputs=[
            "pre_registration",
            "research_plan",
            "hypothesis_innovation_report",
            "hypothesis_research_overview",
        ],
    )
    return HookContext(
        state=state,
        harness=harness,
        turn=3,
        messages=[],
        tool_call_records=[],
    )


def test_normalize_rewrites_wrapped_capital_basis(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    saved = ctx.state.save_artifact(
        "pre_registration",
        "Si_Prereg",
        "# draft",
        metadata={
            "capital_basis": {"item": ["claim_aaa", "claim_bbb"]},
            "execution_commitment": {"substitutes": "", "deferred": ""},
        },
    )
    ctx.tool_call_records = [{
        "name": "save_artifact",
        "args": {
            "artifact_type": "pre_registration",
            "name": "Si_Prereg",
            "metadata": {"capital_basis": {"item": ["claim_aaa", "claim_bbb"]}},
        },
        "result": {"status": "success", "id": saved["id"]},
    }]

    asyncio.run(hyp_hooks._fix_capital_basis_metadata_on_turn_end(ctx))

    rec = ctx.state.read_artifact(saved["id"]) or {}
    assert rec["metadata"]["capital_basis"] == ["claim_aaa", "claim_bbb"]
    assert rec["metadata"]["execution_commitment"]["substitutes"] == {}
    assert rec["metadata"]["execution_commitment"]["deferred"] == []


def test_freeze_capital_fail_sets_nudge_and_overview_hint(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    saved = ctx.state.save_artifact(
        "pre_registration",
        "Si_Prereg",
        "# draft",
        metadata={"capital_basis": {"item": ["claim_aaa"]}},
    )
    freeze_err = {
        "status": "error",
        "error": "metadata.capital_basis 未声明。本项目已有 6 条承重结论",
    }

    async def _run_once(meta: dict) -> None:
        ctx.state.save_artifact(
            "pre_registration", "Si_Prereg", "# draft", metadata=meta,
        )
        ctx.tool_call_records = [{
            "name": "freeze_and_register",
            "args": {"artifact_id": saved["id"]},
            "result": freeze_err,
        }]
        with patch.object(
            hyp_hooks.tool_registry,
            "execute",
            new=AsyncMock(return_value=freeze_err),
        ):
            await hyp_hooks._fix_capital_basis_metadata_on_turn_end(ctx)

    asyncio.run(_run_once({"capital_basis": {"item": ["claim_aaa"]}}))
    assert ctx.state.hook_state.get(hyp_hooks._CAPITAL_BASIS_NUDGE_KEY)
    assert ctx.state.hook_state.get(hyp_hooks._FREEZE_CAPITAL_FAILS_KEY) == 1
    rec = ctx.state.read_artifact(saved["id"]) or {}
    assert rec["metadata"]["capital_basis"] == ["claim_aaa"]

    asyncio.run(_run_once({}))
    assert ctx.state.hook_state.get(hyp_hooks._FREEZE_CAPITAL_FAILS_KEY) >= 2
    assert ctx.state.hook_state.get(hyp_hooks._OVERVIEW_AFTER_FREEZE_NUDGE_KEY)

    msgs = hyp_hooks._missing_outputs_nudge_on_turn_start(ctx)
    assert msgs
    text = msgs[0].content
    assert "capital_basis" in text
    assert "hypothesis_research_overview" in text
