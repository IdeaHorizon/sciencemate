"""A work order is evidence for the task, not an immutable research ontology."""
import asyncio
import copy
import json

import pytest

from core.bootstrap import bootstrap
from core.context_engine import _write_first_turn_node_inputs_receipt
from core.state import State
from core.tool_registry import execute
from nodes.analysis.tests.test_analysis_mode import _seed_round_one
from nodes.analysis.tools.definition_lock import build_definition_lock, resolve_locked_definitions


# The audit's real revise #37 work-list shape. No actual research session is loaded.
STATUS_REFRESH = """revise #37：只修 reviewer v36 列的事实刷新，不动任何裁决
① Q1/Q2 数据已在本机：更新路径、sha256 与 VERIFY.md 复算值
② Q4 段自相矛盾要统一：仅缺 model4b，区分 dev-size 链与 model 链
③ model4b 运行态：首作业超时，续跑作业在跑，勿重复提交
④ s19 段整体改到90键：90/90齐全，正式统计须从90行重建并记来源
"""
TAXONOMY = [{"label": "感知操作", "definition": "从环境采集观测并写入工作记忆"},
            {"label": "规划操作", "definition": "基于目标生成下一步动作序列"}]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_FRAMEWORK_ORG_HOME", str(tmp_path / "home/org"))
    bootstrap()


@pytest.mark.parametrize("prompt", [STATUS_REFRESH,
    "1. 更新图题：只刷新状态\n2. 修改路径：文件已在本机",
    "temperature: 300 K\nruntime_status: running\nseed: 19",
    "## 操作类别\n- 感知操作：采集环境观测\n- 规划操作：生成动作序列",
])
def test_free_text_is_never_promoted_to_definition_authority(prompt):
    lock = build_definition_lock({}, user_prompt=prompt)
    assert lock["locked_definitions"] == []
    assert lock["n_locked"] == 0


def test_generic_categories_are_candidates_not_lock_intent():
    lock = build_definition_lock({"categories": ["done", "todo"],
                                  "taxonomy": {"status": "running"}})
    assert lock["locked_definitions"] == []


def test_explicit_taxonomy_has_input_source_and_request_scope():
    lock = build_definition_lock({"locked_taxonomy": TAXONOMY}, user_prompt=STATUS_REFRESH)
    assert lock["locked_labels"] == [e["label"] for e in TAXONOMY]
    assert all(e["source"] == "node_inputs.locked_taxonomy" and e["scope"] == "request"
               for e in lock["locked_definitions"])


def test_real_tool_chain_does_not_require_refreezing_status_work_items(tmp_path):
    state = State.new(node_type="hypothesis", base_dir=tmp_path / "run")
    _seed_round_one(state)
    state.hook_state["node_inputs"] = {"research_question": STATUS_REFRESH}
    before = {a["id"]: state.read_artifact(a["id"])["content"]
              for a in state.list_artifacts("pre_registration")}
    goal = asyncio.run(execute("get_research_goal", state))
    assert goal["locked_definitions"] == []
    assert "hypothesis_innovation_report" not in "\n".join(
        c for c in goal["checklist"] if c.startswith("【完成闸优先】"))
    audit = asyncio.run(execute("audit_definition_fidelity", state, save_report=False))
    result = asyncio.run(execute("validate_analysis_outputs", state))
    assert audit["passed"] and not audit["applicable"]
    assert result["mode"] == "revise" and result["passed"], result
    assert "definition_fidelity" in result["not_applicable"]
    after = {a["id"]: state.read_artifact(a["id"])["content"]
             for a in state.list_artifacts("pre_registration")}
    assert after == before
    from nodes.analysis.tools.analysis_mode import run_authored_commitments
    assert not run_authored_commitments(state)


@pytest.mark.parametrize("cache_key", ["research_goal_parsed", "dialogue_context"])
def test_restored_legacy_cache_cannot_recreate_an_ontology(tmp_path, cache_key):
    state = State.new(node_type="hypothesis", base_dir=tmp_path / "run")
    legacy = {"locked_definitions": [{"label": "revise #37", "definition": "刷新事实", "source": "user_prompt"}],
              "locked_labels": ["revise #37"], "user_prompt": STATUS_REFRESH}
    state.hook_state[cache_key] = copy.deepcopy(legacy)
    assert resolve_locked_definitions(state) == []
    assert state.hook_state[cache_key] == legacy  # no historical evidence rewritten


def test_resume_reuses_explicit_input_receipt_not_derived_cache(tmp_path):
    state = State.new(node_type="hypothesis", base_dir=tmp_path / "run")
    _write_first_turn_node_inputs_receipt(state, {"locked_taxonomy": TAXONOMY})
    state.hook_state["node_inputs"] = {"research_question": STATUS_REFRESH}
    state.hook_state["research_goal_parsed"] = {"locked_definitions": [{"label": "revise #37"}]}
    # Restore the same run directory and a JSON-round-tripped legacy checkpoint.
    checkpoint = json.loads(json.dumps(state.hook_state))
    state = State.reopen("hypothesis", tmp_path / "run", state.run_id)
    state.hook_state.update(checkpoint)
    locked = resolve_locked_definitions(state)
    assert [e["label"] for e in locked] == [e["label"] for e in TAXONOMY]
    assert all(e["source"].startswith("first_turn_node_inputs.") for e in locked)
    goal = asyncio.run(execute("get_research_goal", state))
    assert goal["locked_labels"] == [e["label"] for e in TAXONOMY]


def test_explicit_definitions_are_enforced_at_real_dispatch(tmp_path):
    state = State.new(node_type="hypothesis", base_dir=tmp_path / "run")
    state.hook_state["node_inputs"] = {"locked_taxonomy": TAXONOMY}
    state.save_artifact("pre_registration", "draft", "Replaced taxonomy, with no user categories")
    audit = asyncio.run(execute("audit_definition_fidelity", state, save_report=False))
    validation = asyncio.run(execute("validate_analysis_outputs", state,
                                    checks=["definition_fidelity"], save_report=False))
    assert not audit["passed"] and audit["applicable"]
    assert validation["failed_checks"] == ["definition_fidelity"]


def test_actual_node_resume_finishes_without_a_new_preregistration(tmp_path):
    from core.executor import execute_node
    from core.loader import load_harness
    from core.llm import LLMResponse

    state = State.new(node_type="hypothesis", base_dir=tmp_path / "runs")
    _seed_round_one(state)
    before = {a["id"]: state.read_artifact(a["id"])["content"]
              for a in state.list_artifacts("pre_registration")}

    class ScriptedModel:
        def __init__(self):
            self.steps = iter([
                ("get_research_goal", {}),
                ("update_research_state", {"verdict": "continue",
                    "hypotheses": [{"id": "H1", "status": "active"}],
                    "change_reason": "刷新运行事实，不改变科学定义或裁决"}),
                ("validate_analysis_outputs", {}),
                None,
            ])

        async def chat(self, messages, **kwargs):
            step = next(self.steps)
            if step is None:
                return LLMResponse(content="已刷新研究状态；沿用冻结协议。", tool_calls=[],
                                   finish_reason="stop", usage={})
            name, args = step
            return LLMResponse(content=None, tool_calls=[{
                "id": "call_" + name, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}],
                finish_reason="tool_calls", usage={})

    harness = load_harness("hypothesis")
    harness.max_turns = 6
    result = asyncio.run(execute_node("hypothesis", state_dir=tmp_path / "runs",
        resume_run_id=state.run_id, node_inputs={"research_question": STATUS_REFRESH},
        llm=ScriptedModel(), harness_override=harness))
    assert result["status"] == "completed", result
    reopened = State.reopen("hypothesis", tmp_path / "runs", state.run_id)
    after = {a["id"]: reopened.read_artifact(a["id"])["content"]
             for a in reopened.list_artifacts("pre_registration")}
    assert after == before
    assert not any(json.loads(line).get("name") == "freeze_artifact"
                   for line in reopened.transcript_path.read_text().splitlines() if line.strip())


def test_revise_nudge_reads_the_same_output_contract_as_executor(tmp_path):
    from core.loader import load_harness
    from core.loop_hooks import HookContext
    from nodes.analysis.hooks import _completion_gate_snapshot
    state = State.new(node_type="hypothesis", base_dir=tmp_path / "run")
    _seed_round_one(state)
    harness = load_harness("hypothesis")
    snapshot = _completion_gate_snapshot(HookContext(harness, state, [], turn=8))
    assert snapshot["required"] == harness.required_outputs_for({"mode": "revise"}) == ["research_state"]


def test_tool_arguments_cannot_create_a_second_definition_authority(tmp_path):
    state = State.new(node_type="hypothesis", base_dir=tmp_path / "run")
    state.save_artifact("pre_registration", "existing", "An unchanged scientific definition")
    audit = asyncio.run(execute("audit_definition_fidelity", state,
                               locked_definitions=[{"label": "work item", "definition": "refresh state"}],
                               save_report=False))
    assert audit["passed"] and not audit["applicable"]
    assert audit["locked_definitions"] == []
