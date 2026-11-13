"""Reported user-facing failures at shared tool and pause boundaries."""

import importlib.util
import json
from pathlib import Path

import pytest

from core.pause import PausedRunContext, PauseEvent
from core.pause_dialogue import answer_from_dialogue, exact_pause_answer
from core.state import State


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("批准执行", "批准执行"),
        ("1", "批准执行"),
        ("[1]", "批准执行"),
        ("拒绝", "拒绝"),
        ("2", "拒绝"),
        ("3", None),
        ("能否解释为什么需要批准", None),
        ("尚未批准执行", None),
    ],
)
def test_legacy_card_consumes_only_its_presented_choices(tmp_path, answer, expected):
    pause = PauseEvent(
        question="Confirm",
        options=["批准执行", "拒绝"],
        asking_run_id="child",
        metadata={"type": "highrisk_confirm"},
    )
    assert exact_pause_answer(pause, answer) == expected
    state = State.new("_orchestrator", tmp_path)
    state.hook_state["_pause_dialogue"] = {"pause": pause, "text": answer}
    result = answer_from_dialogue(
        state, child_run_id="wrong", offer_id="", choice_id="", content=answer
    )
    assert result["status"] == "error"
    result = answer_from_dialogue(
        state, child_run_id="child", offer_id="", choice_id="", content=answer
    )
    assert result["status"] == ("success" if expected else "error")


@pytest.mark.asyncio
async def test_auto_and_person_answers_keep_distinct_model_visible_provenance(
    tmp_path, monkeypatch
):
    from core import agent_loop
    from core.llm import LLMMessage
    from core.pause_driver import AutomaticAnswer

    async def loop(*args):
        return agent_loop.LoopResult(final_text="ok", turns=1, messages=args[2])

    monkeypatch.setattr(agent_loop, "run_loop", loop)
    payloads = []
    for answer in [AutomaticAnswer("2"), "2"]:
        state = State.new("_orchestrator", tmp_path)
        ctx = PausedRunContext(
            run_id=state.run_id,
            state=state,
            messages=[LLMMessage(role="tool", tool_call_id="question", content="paused")],
            harness=None,
            llm=None,
            pending_tool_call_id="question",
            pause_event=PauseEvent(question="Choose", asking_run_id=state.run_id),
        )
        await agent_loop.resume_loop(ctx, answer)
        payloads.append(json.loads(ctx.messages[0].content))
    assert [p["response"] for p in payloads] == ["2", "2"]
    assert [p["answered_by"] for p in payloads] == ["auto", "person"]
    assert [p["automatic"] for p in payloads] == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("target,same", [("experiment", True), ("analysis", False)])
async def test_revise_inherits_exact_dispatch_only_for_same_producer(
    tmp_path, monkeypatch, target, same
):
    import shared.tools.run_node as run_node
    from core.pause_driver import execute_authorized_action

    state = State.new("_orchestrator", tmp_path)
    original = {
        "node_type": "experiment",
        "task_instance_uuid": "task-uuid",
        "task_contract_digest": "immutable-digest",
        "deliverable": {"artifact_type": "pre_registration", "path": "prereg.md"},
    }
    seen = {}

    async def dispatch(state, node_type, **kwargs):
        seen.update(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(run_node, "_run_node_tool", dispatch)
    entry = {
        "decision_state": "action_authorized",
        "producing_node": "experiment",
        "authorized_target_node": target,
        "authorized_action": "revise",
        "producing_node_inputs": {"experiment_spec": {"question": "original"}},
        "producing_dispatch_contract": original,
    }
    assert (await execute_authorized_action(state, entry))["status"] == "success"
    assert seen["node_inputs"]["experiment_spec"] == {"question": "original"}
    for key in ("task_instance_uuid", "task_contract_digest", "deliverable"):
        assert (seen.get(key) == original[key]) if same else key not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["a\nb\n", "a\r\nb\r\n", "试验\n", "\ufeffa\n", "ab", "a\rb"])
async def test_write_file_receipt_matches_actual_utf8_bytes(tmp_path, content):
    from shared.tools.builtin import _write_file

    state = State.new("_orchestrator", tmp_path)
    result = await _write_file(state, "result.txt", content)
    data = Path(result["path"]).read_bytes()
    assert data == content.encode("utf-8")
    assert result["bytes_written"] == len(data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listed,requested",
    [
        ("references/method.md", "references/method.md"),
        ("references\\method.md", "references/method.md"),
        ("references/method.md", "references\\method.md"),
    ],
)
async def test_skill_asset_ids_are_cross_platform_without_expanding_membership(
    tmp_path, monkeypatch, listed, requested
):
    from core import skill_registry
    from core.skill_registry import Skill
    from shared.tools.library.skill_tools import _load_skill

    root = tmp_path / "skill"
    (root / "references").mkdir(parents=True)
    (root / "references/method.md").write_text("方法", encoding="utf-8")
    name = "backlog-asset"
    monkeypatch.setitem(
        skill_registry._SKILLS,
        name,
        Skill(
            name=name,
            description="fixture",
            body_markdown="",
            source_dir=str(root),
            assets=[listed],
        ),
    )
    state = State.new("experiment", tmp_path / "runs")
    out = await _load_skill(state, name=name, asset=requested)
    assert out["status"] == "success", out
    assert out["content"] == "方法"
    for bad in ["../outside.md", "/etc/passwd", "references/unlisted.md"]:
        assert (await _load_skill(state, name=name, asset=bad))["status"] == "error"


@pytest.mark.parametrize("kind", ["missing", "unfinished", "finished"])
def test_ci_report_distinguishes_missing_and_unfinished_logs(tmp_path, kind):
    spec = importlib.util.spec_from_file_location(
        "backlog_reporter", ".gitea/report_pytest_failure.py"
    )
    reporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reporter)
    path = tmp_path / "pytest.log"
    if kind != "missing":
        path.write_text("1 passed in 0.12s\n" if kind == "finished" else "collecting ...\n")
    result = reporter.build_body("failed", logs=(("framework", path),), attachments=())
    assert "日志都跑完了" not in result
    if kind == "missing":
        assert "未取得" in result
    elif kind == "unfinished":
        assert "没有写出收尾行" in result
    else:
        assert "包含收尾行" in result


@pytest.mark.parametrize(
    "command,denied",
    [
        ("cp tilepack/out/summary.json ./report_copy.json", False),
        ("cp report.json ./ordinary_summary.json", False),
        ("cp report.json artifacts/result.json", True),
        ("cp report.json .research/runtime/runs/demo/summary.json", True),
        ("cp report.json /state/projects/p/runs/demo/summary.json", True),
    ],
)
def test_ordinary_summary_filename_is_not_framework_identity(command, denied):
    from shared.lib.dangerous_commands import match_boundary_violation

    assert bool(match_boundary_violation(command)) is denied


@pytest.mark.asyncio
async def test_finalized_child_carries_dispatch_contract_into_review_flow(tmp_path):
    from core.agent_loop import LoopResult
    from core.executor import finalize_run
    from core.harness import NodeHarness
    from shared.tools.run_node import _finish_child

    parent = State.new("_orchestrator", tmp_path / "runs")
    child = State.new(
        "analysis",
        tmp_path / "runs",
        task_instance_uuid="exact-task",
        task_contract_digest="exact-revision",
    )
    child.hook_state["_deliverable_projection"] = {
        "artifact_type": "pre_registration",
        "path": "prereg.md",
    }
    child.save_artifact("pre_registration", "prereg", "protocol")
    harness = NodeHarness(
        node_type="analysis", required_output_artifact_types=["pre_registration"]
    )
    summary = await finalize_run(
        child, harness, LoopResult(final_text="done", turns=1, messages=[]), None
    )
    _finish_child(parent, "analysis", {"research_goal": "original"}, summary, harness, None)
    flow = parent.hook_state["pending_post_node_flow"][-1]
    assert flow["producing_dispatch_contract"] == {
        "node_type": "analysis",
        "task_instance_uuid": "exact-task",
        "task_contract_digest": "exact-revision",
        "deliverable": {"artifact_type": "pre_registration", "path": "prereg.md"},
    }
