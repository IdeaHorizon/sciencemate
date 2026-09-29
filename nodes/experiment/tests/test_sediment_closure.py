"""Regression tests for experiment-local sediment closure recovery."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from core.ledger import sha256_text
from core.loop_hooks import HookContext
from core.state import State
import shared.tools.library.kb as kb
from nodes.experiment import hooks
from nodes.experiment.tools import sediment
from nodes.experiment.tools.contract_audit import (
    TERMINAL_CLOSURE_REGISTRY,
    audit_sediment,
)


def _state(tmp_path: Path) -> State:
    state = State.new("experiment", tmp_path)
    state.hook_state["run_contract"] = {"run_role": "primary", "stage": "simulation"}
    return state


def _save_log(state: State, *, frozen: bool = False, auto_generated: bool = False) -> str:
    saved = state.save_artifact(
        "experiment_log", "run_log",
        "## Hypothesis Verdict\nverdict: inconclusive\n"
        "reason: platform validation without a scientific hypothesis\n\n"
        "## Credibility\ncredibility: questionable\n",
        metadata={"auto_generated": auto_generated},
    )
    if frozen:
        # 冻结只能来自账本的 freeze 行（save 行里的 frozen 键会被剥掉）。
        state.mark_frozen(saved["id"])
    return saved["id"]


def _record_tool(state: State, name: str, args: dict, result: dict) -> None:
    state.append_transcript("tool_call", name=name, args=args)
    state.append_transcript("tool_result", name=name, result_preview=result)


def _candidate(*, sources: list[str]) -> dict:
    return {
        "claim_text": "The launcher check prevents a repeatable MPI startup mismatch.",
        "claim_type": "methodological",
        "sources": sources,
        "orphan_reason": "This project has not yet registered a reusable concept.",
        "confidence": 0.6,
    }


def _complete_contract_audit(**overrides: dict) -> dict[str, dict]:
    """Return a complete passing terminal surface, then apply test-local deltas."""
    result = {
        key: {"passed": True, "applicable": True, "reason": "ok"}
        for key in TERMINAL_CLOSURE_REGISTRY
    }
    result["verdict"]["late_declaration"] = False
    result["sediment"]["late_declaration"] = False
    result.update({
        "execution_record": {"passed": True, "reason": "ok"},
        "terminal_failure_record": {"applicable": False, "passed": False},
    })
    result.update(overrides)
    return result


def test_assess_uses_shared_schema_and_reserves_additional_log_chunk() -> None:
    """零证据的候选不 admissible —— 判据是**节点自己的**，不再借 KB 的闸。

    原来这条断言的是 `independent_source_count`（KB schema 的 HIGH_TIER 闸，
    要求独立来源 ≥ 2）。那道闸已删：它教模型改 claim_type 过门，而类型是
    晋升分道的路由键。而且它答的是另一个问题 —— 节点想问"这条有没有证据"，
    闸答的是"独立来源够不够两个"。

    现在的判据：预留的 log chunk 是**承诺**（freeze/register 之后才存在），
    不是证据。只有它一个来源 = 零证据。
    """
    failed = asyncio.run(sediment._assess_sediment_candidate(
        None, _candidate(sources=[])))
    assert failed["status"] == "success"
    assert failed["admissible"] is False
    assert "承诺不是证据" in failed["schema_error"]
    assert sediment._FUTURE_LOG_CHUNK in failed["schema_error"]
    assert failed["unverified"] == ["future_experiment_log_chunk_existence"]

    passed = asyncio.run(sediment._assess_sediment_candidate(
        None, _candidate(sources=["https://example.org/mpi-evidence"])))
    assert passed["status"] == "success"
    assert passed["admissible"] is True
    assert passed["closure_effect"] == "sediment_claim"
    assert passed["unverified"] == ["future_experiment_log_chunk_existence"]


def test_declare_before_freeze_renders_log_and_closes_audit(tmp_path: Path) -> None:
    state = _state(tmp_path)
    log_id = _save_log(state)
    args = {"reason": "本次结果是单次平台验证，没有可复用的方法学或真实失败经验。"}
    state.append_transcript("tool_call", name="declare_no_sediment", args=args)
    result = asyncio.run(sediment._declare_no_sediment(state, **args))
    state.append_transcript("tool_result", name="declare_no_sediment", result_preview=result)

    assert result["status"] == "success"
    assert result["experiment_log_id"] == log_id
    assert result["rendered_in_log"] is True
    log = state.read_artifact(log_id)
    assert "本次未发现可进入 KB 的 methodological / dead_end finding" in log["content"]
    audit = audit_sediment(state)
    assert audit["passed"] is True
    assert audit["has_explicit_none"] is True


def test_declaration_naming_an_older_log_binds_to_it_and_witnesses_not_latest(tmp_path: Path) -> None:
    """判决拆除·第三波（sed:161/648 降格）：点名真实但非最新的 experiment_log 不再
    拒绝——挂到点名的那份并见证 not_latest；墙加回去（status=error）即转红。"""
    state = _state(tmp_path)
    older = _save_log(state)
    newer = state.save_artifact("experiment_log", "run_log_v2", "## Credibility\ncredibility: questionable\n",
                                metadata={})["id"]
    assert older != newer

    result = asyncio.run(sediment._declare_no_sediment(
        state, reason="older log carries the whole record", experiment_log_id=older))
    assert result["status"] == "success", result
    assert result["experiment_log_id"] == older
    assert result["closure_witness"]["not_latest"] == {
        "declared_experiment_log_id": older, "latest_experiment_log_id": newer}
    assert "本次未发现可进入 KB" in state.read_artifact(older)["content"]
    assert "本次未发现可进入 KB" not in state.read_artifact(newer)["content"]

    # 同一绑定规则也管 declare_inconclusive_verdict（sed:648）。渲染进 older 是一次
    # amend（路径即身份），older 因此又成了「最新」——换一个干净 state 验。
    state2 = _state(tmp_path / "second")
    older2 = _save_log(state2)
    newer2 = state2.save_artifact("experiment_log", "run_log_v2", "## Credibility\ncredibility: questionable\n",
                                  metadata={})["id"]
    verdict = asyncio.run(sediment._declare_inconclusive_verdict(
        state2, reason="same binding rule for the verdict declaration",
        next_step="rerun with the newer log", experiment_log_id=older2))
    assert verdict["status"] == "success", verdict
    assert verdict["experiment_log_id"] == older2
    assert verdict["closure_witness"]["not_latest"] == {
        "declared_experiment_log_id": older2, "latest_experiment_log_id": newer2}

    unknown = asyncio.run(sediment._declare_no_sediment(
        state, reason="id that is not a log of this run", experiment_log_id="experiment_log__nope"))
    assert unknown["status"] == "success"
    assert unknown["closure_witness"]["experiment_log_not_found"]["declared_experiment_log_id"] == "experiment_log__nope"


def test_declare_after_freeze_is_bound_addendum_and_closes_audit(tmp_path: Path) -> None:
    state = _state(tmp_path)
    log_id = _save_log(state, frozen=True)
    original = state.read_artifact(log_id)["content"]
    args = {"reason": "候选方法学结论只有单一来源，不能诚实地作为长期复用结论。"}
    state.append_transcript("tool_call", name="declare_no_sediment", args=args)
    result = asyncio.run(sediment._declare_no_sediment(state, **args))
    state.append_transcript("tool_result", name="declare_no_sediment", result_preview=result)

    assert result["status"] == "success"
    assert result["log_frozen"] is True
    assert result["rendered_in_log"] is False
    assert state.read_artifact(log_id)["content"] == original
    audit = audit_sediment(state)
    assert audit["passed"] is True
    assert audit["has_explicit_declaration"] is True
    assert audit["late_declaration"] is True


def test_auto_generated_log_declaration_is_recorded_with_weak_evidence_witness(tmp_path: Path) -> None:
    """判决拆除 O6（sed:167 降格，2026-08-31）。

    auto_generated 兜底 log 上的声明照记（不再拒绝），弱证据机械标注
    （closure_witness.experiment_log_auto_generated）；审计照实不放行 ——
    检查存活为见证，拒绝分支死。
    """
    state = _state(tmp_path)
    _save_log(state, auto_generated=True)
    result = asyncio.run(sediment._declare_no_sediment(
        state, reason="框架自动记录不是 agent 研究结论，不能作为沉积闭环证据。"))
    assert result["status"] == "success", result
    assert result["closure_witness"]["experiment_log_auto_generated"] is True
    audit = audit_sediment(state)
    assert audit["passed"] is False
    assert audit["auto_generated_record"] is True


def test_rejected_methodological_claim_gets_node_specific_recovery(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _save_log(state, frozen=True)
    ctx = HookContext(
        harness=None, state=state, messages=[], turn=7,
        tool_call_records=[{
            "name": "create_claim",
            "args": {"claim_type": "methodological"},
            "result": {"status": "error", "error": "sediment 候选没有任何真实来源"},
        }],
    )
    messages = hooks.sediment_closure_advisor_on_turn_end(ctx)
    text = "\n".join(message.content for message in messages or [])
    assert "empirical" in text
    assert "declare_no_sediment" in text
    assert "不要为过门虚构 dead_end" in text
    assert any(event.get("event") == "sediment_methodological_rejected_guidance"
               for event in _events(state))


def test_end_footer_covers_sediment_not_only_verdict(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)
    _save_log(state)
    result = _complete_contract_audit(
        sediment={"passed": False, "reason": "missing sediment", "late_declaration": False},
    )
    monkeypatch.setattr(hooks, "audit_experiment_contract", lambda _state: result)
    loop_result = SimpleNamespace(final_text="模型称已完成")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=2), loop_result)

    assert "## Framework Mechanical Status" in loop_result.final_text
    assert "overall_status: incomplete" in loop_result.final_text
    assert "experiment_sediment_audit: failed" in loop_result.final_text
    assert loop_result.status == "blocked"
    assert state.hook_state["experiment_downstream_blocked"]["review_eligibility"] is False
    assert any(event["event"] == "experiment_downstream_blocked" for event in _events(state))


def test_end_footer_exposes_successful_late_declaration(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)
    _save_log(state, frozen=True)
    result = _complete_contract_audit(
        sediment={"passed": True, "reason": "declared", "late_declaration": True},
    )
    monkeypatch.setattr(hooks, "audit_experiment_contract", lambda _state: result)
    loop_result = SimpleNamespace(final_text="模型完成")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=2), loop_result)

    assert "## Framework Closure Addendum" in loop_result.final_text
    assert "after experiment_log freeze" in loop_result.final_text


def test_preview_reuses_contract_audit(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)
    expected = _complete_contract_audit(
        sediment={"passed": False, "reason": "missing sediment"},
    )
    monkeypatch.setattr(sediment, "audit_experiment_contract", lambda _state: expected)
    preview = asyncio.run(sediment._preview_experiment_contract(state))
    assert preview["overall_status"] == "incomplete"
    assert preview["failed_checks"] == ["sediment"]


def _events(state: State) -> list[dict]:
    import json
    return [json.loads(line) for line in state.transcript_path.read_text().splitlines()]

class _ResolverState:
    def __init__(self, *, measured: bool = True):
        self.prereg_id = "pre_registration__bound"
        self.hook_state = {
            "node_inputs": {"prereg_artifact_id": self.prereg_id},
        }
        self.prereg = {
            "name": "bound", "metadata": {"frozen": True},
            "content": (
                "## Hypothesis 2 (H2): attenuation\n"
                "### Falsification Criteria\n```yaml\n"
                "metric: attenuation_ratio\ncomparison: greater_than\nthreshold: 2\n```\n"
            ),
        }
        self.log = {"metadata": {"measured_metrics": {
            "attenuation_ratio": {"status": "measured" if measured else "estimated", "value": 2.4}
        }}}
        self.claim = {
            "id": "claim_h2_bound", "claim_type": "hypothesis", "status": "open",
            "prereg_chunk_id": "chunk_prereg_bound",
            "scope_dimensions": {"prereg_hypothesis_id": "H2"},
            "falsification_criteria_text": "attenuation_ratio > 2",
        }

    def list_artifacts(self, artifact_type=None):
        rows = [
            {"id": self.prereg_id, "type": "pre_registration"},
            {"id": "experiment_log__bound", "type": "experiment_log"},
        ]
        return [row for row in rows if artifact_type is None or row["type"] == artifact_type]

    def read_artifact(self, artifact_id):
        if artifact_id == self.prereg_id:
            return self.prereg
        if artifact_id == "experiment_log__bound":
            return self.log
        return None

    def list_kb(self, entity, **_kwargs):
        if entity == "chunks":
            return [{"id": "chunk_prereg_bound", "origin_artifact_id": self.prereg_id,
                     "origin_artifact_frozen": True,
                     "origin_content_hash": sha256_text(self.prereg["content"])}]
        return [self.claim] if entity == "claims" else []

    def get_kb_record(self, entity, kb_id):
        return self.claim if entity == "claims" and kb_id == self.claim["id"] else None


def test_resolve_prereg_hypotheses_uses_provenance_not_keyword_search() -> None:
    state = _ResolverState()
    result = asyncio.run(sediment._resolve_prereg_hypotheses(state))

    assert result["resolution_status"] == "resolved"
    assert result["prereg_chunk_id"] == "chunk_prereg_bound"
    assert result["hypotheses"] == [{
        "hypothesis_id": "H2", "claim_ids": ["claim_h2_bound"],
        "claim_id": "claim_h2_bound", "resolution": "bound",
        "commitment": result["hypotheses"][0]["commitment"],
    }]


def test_resolve_prereg_questions_supports_non_hypothesis_statement_closure() -> None:
    state = _ResolverState()
    state.prereg["content"] = (
        "## Research Questions\n"
        "### Q1: map the stable phase region\n"
        "- output_kind: phase map\n"
        "- assumption: the sampled range contains the relevant phase boundary\n"
        "```yaml\n"
        "- id: COVERAGE\n"
        "  statement: temperature 100-400 K and composition 0.0-1.0 were fully scanned\n"
        "- metric: convergence_rate\n"
        "  comparison: >=\n"
        "  threshold: 0.95\n"
        "```\n"
    )
    state.claim = None

    result = asyncio.run(sediment._resolve_prereg_questions(state))

    assert result["resolution_status"] == "resolved"
    assert result["prereg_chunk_id"] == "chunk_prereg_bound"
    assert len(result["questions"]) == 1
    question = result["questions"][0]
    assert question["question_id"] == "Q1"
    assert question["is_hypothesis"] is False
    assert question["claim_resolution"] == "not_applicable"
    assert question["closure_items"] == [
        {"id": "COVERAGE", "kind": "statement",
         "description": "`COVERAGE` temperature 100-400 K and composition 0.0-1.0 were fully scanned",
         "record_in": "closure_discharges",
         "statement": "temperature 100-400 K and composition 0.0-1.0 were fully scanned"},
        {"id": "convergence_rate", "kind": "numeric",
         "description": "`convergence_rate` >= 0.95", "record_in": "measured_metrics",
         "metric": "convergence_rate", "comparison": ">=", "threshold": "0.95"},
    ]


def test_sediment_reads_the_contract_bound_snapshot_across_amendment_and_refreeze(tmp_path: Path) -> None:
    """A legal v1-bound run must not read an unfrozen v2 draft.

    This is the experiment half of the RFC replay: freeze v1, start an
    amendment, explicitly bind a run to v1, then refreeze v2. Sediment must
    select the same version as the run manifest at both points.
    """
    state = _state(tmp_path)
    v1 = state.save_artifact(
        "pre_registration", "versioned_questions",
        "## Research Questions\n### Q1: v1 question\n- output_kind: scalar\n```yaml\n- metric: score\n  comparison: >=\n  threshold: 1\n```\n",
        metadata={},
    )
    prereg_id = v1["id"]
    state.mark_frozen(prereg_id, {"freeze_reason": "v1 preregistration"})
    chunk_v1 = asyncio.run(kb._kb_register_artifact_as_chunk(state, prereg_id))

    state.save_artifact(
        "pre_registration", "versioned_questions",
        "## Research Questions\n### Q1: v2 question\n- output_kind: scalar\n```yaml\n- metric: score\n  comparison: >=\n  threshold: 2\n```\n",
        metadata={}, amendment_reason="tighten the preregistered threshold",
    )
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id, "prereg_version": 1, "stage": "simulation",
    }
    old = asyncio.run(sediment._resolve_prereg_questions(state))
    assert old["resolution_status"] == "resolved"
    assert old["prereg_chunk_id"] == chunk_v1["chunk_id"]

    # 重新冻结 v2：账本上一行 freeze，文件一个字节不动。
    state.mark_frozen(prereg_id, {"freeze_reason": "refreeze the amended v2"})
    chunk_v2 = asyncio.run(kb._kb_register_artifact_as_chunk(state, prereg_id))

    state.hook_state["node_inputs"] = {"prereg_artifact_id": prereg_id, "stage": "simulation"}
    new = asyncio.run(sediment._resolve_prereg_questions(state))
    assert new["resolution_status"] == "resolved"
    assert new["prereg_chunk_id"] == chunk_v2["chunk_id"]
    assert chunk_v1["chunk_id"] != chunk_v2["chunk_id"]


def test_real_state_resolves_frozen_question_without_hypothesis_claim(tmp_path: Path) -> None:
    """Smoke path: actual State storage + run-contract-selected frozen prereg."""
    state = _state(tmp_path)
    prereg_id = state.save_artifact(
        "pre_registration", "question_only",
        "## Research Questions\n"
        "### Q1: map the stable phase region\n"
        "- output_kind: phase map\n"
        "- assumption: the sampled range contains the relevant phase boundary\n"
        "```yaml\n"
        "- id: COVERAGE\n"
        "  statement: temperature 100-400 K and composition 0.0-1.0 were fully scanned\n"
        "```\n",
    )["id"]
    state.mark_frozen(prereg_id)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id, "stage": "simulation"}

    result = asyncio.run(sediment._resolve_prereg_questions(state))

    assert result["resolution_status"] == "resolved"
    assert result["pre_registration_id"] == prereg_id
    assert result["claim_resolution_available"] is False
    assert result["questions"][0]["claim_resolution"] == "not_applicable"
    assert result["questions"][0]["closure_items"][0]["id"] == "COVERAGE"


def test_assess_verdict_transition_checks_prereg_measurement_gate() -> None:
    good = asyncio.run(sediment._assess_verdict_transition(
        _ResolverState(), "claim_h2_bound", "H2", "validated",
        "Measured attenuation_ratio=2.4 against the frozen threshold of 2.0.",
    ))
    assert good["admissible"] is True
    assert good["unverified"] == ["future_experiment_log_chunk_existence"]

    blocked = asyncio.run(sediment._assess_verdict_transition(
        _ResolverState(measured=False), "claim_h2_bound", "H2", "validated",
        "Measured attenuation_ratio=2.4 against the frozen threshold of 2.0.",
    ))
    assert blocked["admissible"] is False
    # v0.5：账本单位从「指标」泛化成「闭合条件项」，门禁文案跟着变。
    # 断言认行为不认措辞 —— 拦住了、点名了那条没兑现的、如实标了 estimated。
    assert "没有合格的兑现记录" in blocked["error"]
    assert "attenuation_ratio" in blocked["error"]
    assert "estimated" in blocked["error"]


def test_inconclusive_verdict_after_freeze_closes_verdict_audit(tmp_path: Path) -> None:
    from nodes.experiment.tools.contract_audit import audit_verdict

    state = _state(tmp_path)
    log_id = _save_log(state, frozen=True)
    args = {
        "reason": "本次工程验证没有对应的冻结 hypothesis claim，不能把运行成功当作科学证据。",
        "next_step": "由 hypothesis 节点登记可证伪 claim 后重跑正式实验。",
    }
    _record_tool(state, "declare_inconclusive_verdict", args,
                 asyncio.run(sediment._declare_inconclusive_verdict(state, **args)))

    audit = audit_verdict(state)
    assert audit["passed"] is True
    assert audit["has_explicit_declaration"] is True
    assert audit["late_declaration"] is True
    assert state.read_artifact(log_id)["content"].count("verdict: inconclusive") == 1


def test_end_footer_exposes_late_verdict_declaration(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)
    _save_log(state, frozen=True)
    result = _complete_contract_audit(
        verdict={"passed": True, "reason": "declared", "late_declaration": True},
    )
    monkeypatch.setattr(hooks, "audit_experiment_contract", lambda _state: result)
    loop_result = SimpleNamespace(final_text="模型完成")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=2), loop_result)

    assert "## Framework Closure Addendum" in loop_result.final_text
    assert "verdict: an explicit inconclusive declaration" in loop_result.final_text


def test_end_closure_blocks_missing_frozen_result_evidence_but_not_reviewer_quality(tmp_path: Path, monkeypatch) -> None:
    """Evidence existence is a node gate; credibility assessment remains reviewer/QC work."""
    state = _state(tmp_path)
    _save_log(state)
    result = _complete_contract_audit(
        result_evidence={"passed": False, "reason": "raw_results is not frozen"},
    )
    monkeypatch.setattr(hooks, "audit_experiment_contract", lambda _state: result)
    loop_result = SimpleNamespace(final_text="模型称已完成")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=2), loop_result)
    assert loop_result.status == "blocked"
    assert "experiment_result_evidence_audit: failed" in loop_result.final_text

    # No credibility or general QC score participates in closure_checks.  A
    # questionable result that has real frozen evidence goes to reviewer rather
    # than being converted into an Experiment terminal blocker.
    result["result_evidence"] = {"passed": True, "reason": "frozen raw and clean evidence"}
    loop_result = SimpleNamespace(final_text="模型称已完成")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=3), loop_result)
    assert not hasattr(loop_result, "status")
    assert "## Framework Mechanical Status" not in loop_result.final_text

def test_terminal_closure_blocks_estimated_proxy_even_with_frozen_artifacts(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)
    _save_log(state)
    result = _complete_contract_audit(
        scientific_question_closure={
            "passed": False,
            "reason": "Q2 native_energy is estimated, not measured",
            "status": "unresolved",
        },
    )
    monkeypatch.setattr(hooks, "audit_experiment_contract", lambda _state: result)

    loop_result = SimpleNamespace(final_text="模型称原科学任务已完成")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=2), loop_result)

    assert loop_result.status == "blocked"
    assert "experiment_scientific_question_closure_audit: failed" in loop_result.final_text
    assert state.hook_state["experiment_downstream_blocked"]["reason"] == "experiment_closure_incomplete"


def test_kb_registration_failure_is_reported_but_does_not_block_execution_closure(monkeypatch, tmp_path: Path):
    """A bad optional KB index is visible, but cannot negate frozen execution evidence."""
    state = _state(tmp_path)
    result = SimpleNamespace(status="completed", final_text="completed")
    audit = _complete_contract_audit(
        execution_record={"passed": False, "registration_status": "invalid", "reason": "KB index unavailable"},
    )
    monkeypatch.setattr(hooks, "audit_experiment_contract", lambda _state: audit)

    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1), result)

    assert result.status == "completed"
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert "experiment_kb_registration_audit" in transcript
    assert "registration_status" in transcript


def test_audit_exception_blocks_even_when_transcript_is_unavailable(
    tmp_path: Path, monkeypatch,
) -> None:
    """A second on_end I/O failure must not erase the authoritative blocker."""
    state = _state(tmp_path)

    def audit_boom(_state):
        raise RuntimeError("malformed audit input")

    def transcript_boom(*_args, **_kwargs):
        raise OSError("transcript storage unavailable")

    monkeypatch.setattr(hooks, "audit_experiment_contract", audit_boom)
    monkeypatch.setattr(state, "append_transcript", transcript_boom)
    loop_result = SimpleNamespace(status="completed", final_text="model claimed completion")

    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1), loop_result)

    assert loop_result.status == "blocked"
    assert any(item.get("blocker_id") == "experiment_closure_audit_error"
               for item in state.hook_state["blockers"])


def test_audit_exception_blocker_forces_core_final_status(
    tmp_path: Path, monkeypatch,
) -> None:
    """The persistent node blocker, not loop_result.status, is Core authority."""
    from core.agent_loop import LoopResult
    from core.executor import finalize_run
    from core.harness import NodeHarness

    state = _state(tmp_path)
    original_append = state.append_transcript

    def audit_boom(_state):
        raise RuntimeError("audit source unavailable")

    def transcript_boom(*_args, **_kwargs):
        raise OSError("transcript storage unavailable")

    monkeypatch.setattr(hooks, "audit_experiment_contract", audit_boom)
    monkeypatch.setattr(state, "append_transcript", transcript_boom)
    loop_result = LoopResult(final_text="model claimed completion", turns=1,
                             tool_calls=[], messages=[], status="completed")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1), loop_result)
    monkeypatch.setattr(state, "append_transcript", original_append)

    summary = asyncio.run(finalize_run(
        state, NodeHarness(node_type="experiment", required_outputs=[]),
        loop_result, llm=None,
    ))
    assert summary["status"] == "blocked"
    assert summary["blockers"][0]["blocker_id"] == "experiment_closure_audit_error"
