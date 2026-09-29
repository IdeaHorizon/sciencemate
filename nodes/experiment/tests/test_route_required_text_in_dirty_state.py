"""048: the `execution_route_required` refusal must be walkable on the first read.

Found by walking the 047 whitelist change through a *dirty* run (a canonical route
already frozen). Two prose defects only show up there:

1. the text said a step's action must match the call's ``effects``; but observed
   effects such as ``unknown_executable`` are not in ``ROUTE_EFFECTS`` and cannot be
   declared. Copying the observation literally is rejected by the schema. Route
   matching uses ``observed | declared`` (a union), so only declarable effects
   belong in the step. The text now says so.
2. when a canonical route is already frozen, "declare a route" literally hits
   ``route_amendment_reason_required`` on the next call. The text now names the
   amendment path — and only when a frozen route exists, because on a clean run
   that sentence would be misleading (there is nothing to amend).

048 v2 (Codex review 03): both hints are derived from the draft that is *actually
attached* (``next_action``), never from "a canonical route is readable". A blocked
or interrupted route gets no draft, so it must get neither the "copy the draft"
sentence nor the "amend with amendment_reason" sentence — following either there
would hit ``route_recovery_basis_required`` next.

048 v3 (review): a ``next_action`` advertised as copy-pasteable must pass the same
side-effect-free preflight as the real declare entry. ``_route_step_draft`` now runs
``_route_amendment_preflight`` — the function ``_declare_execution_route`` itself
gates on — instead of a private list of state strings. Two real states that the
string list missed: an ``in_progress`` route (external job submitted, not finalized)
and a run whose operation closure is sealed. Both are exercised here without
monkeypatching the snapshot, and each asserts 同源: the amendment the draft would
have proposed is refused by declare with the very code the preflight guards.
"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

from nodes.experiment.tests.test_execution_route import _state as _classified_state
from nodes.experiment.tests.test_external_route_projection import (
    _route as _external_route,
    _submitted_route,
)
from nodes.experiment.tests.test_route_shadow_wiring import _single_step_route, _state
from nodes.experiment.tools import execution_route, operation_completion, safe_bash

# LD_PRELOAD is judged non-read-only on the frozen base already (no dependence on 047).
_CMD = "LD_PRELOAD=/tmp/evil.so git status --short"


def _first_refusal(state) -> dict:
    probe = safe_bash._bash_route_action(_CMD, execution_stage="diagnostic", state=state)
    action = {
        "tool": "safe_run_bash",
        "program": probe["program"],
        "read_only": probe["read_only"],
        "observed_effects": probe["observed_effects"],
        "workdir_roles": ["managed_source_root"],
        "dry_run": False,
    }
    decision = execution_route.resolve_execution_context(state, action)
    block = execution_route.enforce_execution_route(
        state, action, decision, phase="pre_materialization")
    assert block is not None and block["reason"] == "execution_route_required", block
    return block


def _freeze_unrelated_route(state) -> None:
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job", program="make", role="build_root",
            effects=["workspace_write", "process_tree", "external_job"]),
    ))
    assert declared["status"] == "success", declared


def test_effects_sentence_only_asks_for_declarable_effects(tmp_path):
    block = _first_refusal(_state(tmp_path))
    text = block["error"]
    # The sentence that tells the model what must match no longer lists bare
    # "effects": it says only declarable effects go into the step, and names the
    # observed-but-undeclarable example.
    assert "可声明" in text
    assert "unknown_executable" in text
    # 048 v2: the union is for effective effects / risk, not for matching.
    assert "并集" in text
    assert "匹配按并集" not in text
    assert "step 身份匹配" in text
    # The draft that accompanies the text already filters to ROUTE_EFFECTS.
    draft = block.get("next_action")
    assert isinstance(draft, dict), block
    declared_effects = {
        effect
        for step in ((draft.get("arguments") or {}).get("route") or {}).get("steps", [])
        for effect in step.get("effects", [])
    }
    assert declared_effects <= execution_route.ROUTE_EFFECTS


def test_dirty_state_first_refusal_names_the_amendment_path(tmp_path):
    state = _state(tmp_path)
    _freeze_unrelated_route(state)
    block = _first_refusal(state)
    text = block["error"]
    assert "amendment_reason" in text
    assert "route_amendment_reason_required" in text
    # And the attached draft is an amendment, not a fresh parallel route.
    draft = block.get("next_action") or {}
    assert "amendment_reason" in str(draft), draft


def test_clean_state_first_refusal_does_not_mention_amendment(tmp_path):
    block = _first_refusal(_state(tmp_path))
    assert "amendment_reason" not in block["error"]
    assert "declare_execution_route" in block["error"]


def test_dirty_state_text_is_walkable_literally(tmp_path):
    """Follow the refusal literally: amend the frozen route with the draft's shape
    plus an amendment_reason, retry with route_step_id, and the action proceeds."""
    state = _state(tmp_path)
    _freeze_unrelated_route(state)
    block = _first_refusal(state)
    draft = block["next_action"]
    args = dict(draft["arguments"])
    route = args["route"]
    # 048 v2（Codex 复审）：草稿里是 "<按任务填写…>" 占位符，setdefault 不会替换它——
    # 那样的测试只证明解析器容忍模板。这里照文案"把 fill_in 列出的字段填成真实值"
    # 显式替换，并断言提交参数里不再有任何占位符。
    for step in route["steps"]:
        if step.get("action", {}).get("program") == "git":
            step["goal"] = "按声明的环境读一次仓库状态"
            step["expected_outputs"] = []
            step["workdir_role"] = "managed_source_root"
    submitted = {
        "route": route,
        "amendment_reason": "按拒绝文案追加本步骤：本次调用带 LD_PRELOAD 前缀，被机械判为非只读",
    }
    assert "<按任务填写" not in json.dumps(submitted, ensure_ascii=False)
    assert "<说明为什么" not in json.dumps(submitted, ensure_ascii=False)
    amended = asyncio.run(execution_route._declare_execution_route(state, **submitted))
    assert amended["status"] == "success", amended
    git_step = next(
        s["id"] for s in route["steps"]
        if s.get("action", {}).get("program") == "git")
    probe = safe_bash._bash_route_action(_CMD, execution_stage="diagnostic", state=state)
    action = {
        "tool": "safe_run_bash", "program": probe["program"],
        "read_only": probe["read_only"], "observed_effects": probe["observed_effects"],
        "workdir_roles": ["managed_source_root"], "dry_run": False,
        "route_step_id": git_step,
    }
    decision = execution_route.resolve_execution_context(state, action)
    assert execution_route.enforce_execution_route(
        state, action, decision, phase="pre_materialization") is None, decision


# ── 048 v2（Codex 复审 03 号）：提示只能来自真实附上的草稿 ────────────────────

def _freeze_route_then_block_it(state) -> None:
    """A frozen local route whose only step failed: the route is blocked, so
    ``_route_step_draft`` refuses to offer a draft (it would need recovery_basis)."""
    # A bounded local action (unpack) is what the v2 schema lets safe_run_bash own;
    # a real build would have to be a submit_job step.
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="safe_run_bash", program="tar", role="run_root",
            effects=["process_tree", "workspace_write"]),
    ))
    assert declared["status"] == "success", declared
    action = {
        "tool": "safe_run_bash", "program": "tar", "route_step_id": "execute",
        "read_only": False, "dry_run": False,
        "observed_effects": ["process_tree", "workspace_write"],
        "workdir_roles": ["run_root"], "payload_digest": "d" * 64,
    }
    decision = dict(execution_route.resolve_execution_context(state, action))
    assert decision["decision"] == "matched_ready_step", decision
    decision.update({
        "workdir_role_observed": True, "workdir_resolution_status": "explicit",
        "resolved_workdir": str(state.root),
    })
    assert execution_route.enforce_execution_route(state, action, decision) is None
    binding = execution_route.begin_route_step_attempt(
        state, decision, tool="safe_run_bash", action=action)
    assert binding is not None, decision
    finished = execution_route.finish_route_step_attempt(
        state, binding, result={"status": "error", "returncode": 2, "error": "make failed"})
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot.get("route_state") in {"blocked", "interrupted"}, (finished, snapshot)


def test_blocked_route_refusal_promises_nothing_it_does_not_attach(tmp_path):
    state = _state(tmp_path)
    _freeze_route_then_block_it(state)
    block = _first_refusal(state)
    assert "next_action" not in block, block
    text = block["error"]
    assert "附了" not in text
    assert "amendment_reason" not in text
    assert "route_amendment_reason_required" not in text


def test_clean_route_refusal_only_mentions_the_draft_it_attached(tmp_path):
    block = _first_refusal(_state(tmp_path))
    assert isinstance(block.get("next_action"), dict)
    assert "附了" in block["error"]
    assert "amendment_reason" not in block["error"]


def test_amendment_hint_is_derived_from_the_attached_draft(tmp_path, monkeypatch):
    """Even with a frozen, amendable route, no draft ⇒ no amendment instruction."""
    state = _state(tmp_path)
    _freeze_unrelated_route(state)
    monkeypatch.setattr(execution_route, "_route_step_draft", lambda *_a, **_k: None)
    block = _first_refusal(state)
    assert "next_action" not in block
    assert "amendment_reason" not in block["error"]
    assert "附了" not in block["error"]


# ── 048 v3（复审）：草稿必须过 declare 入口同一份只读预检 ─────────────────────────

# A bounded local unpack: the one effectful shape the v2 schema lets safe_run_bash own,
# so in an *actionable* dirty run it does get an amendment draft (see the control below).
_UNPACK = {
    "tool": "safe_run_bash", "program": "tar", "read_only": False,
    "observed_effects": ["process_tree", "workspace_write"],
    "workdir_roles": ["run_root"], "dry_run": False,
}


def _effectful_refusal(state, action: dict = _UNPACK) -> dict:
    decision = execution_route.resolve_execution_context(state, action)
    block = execution_route.enforce_execution_route(
        state, action, decision, phase="pre_materialization")
    assert block is not None and block["reason"] == "execution_route_required", block
    return block


def _assert_no_draft_and_no_copy_promise(block: dict) -> None:
    assert "next_action" not in block, block
    text = block["error"]
    assert "照抄" not in text, text
    assert "不要新声明一条平行路线" not in text, text
    assert "附了" not in text, text


def _amended_with_step(state, *, program: str, effects: list[str], role: str,
                       tool: str = "safe_run_bash") -> dict:
    """The amendment the draft would have proposed: existing steps verbatim + one more."""
    route = deepcopy(execution_route.load_canonical_route(state)["route"])
    step_id = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in program).strip("_-")
    route["steps"] = [*route["steps"], {
        "id": f"{step_id}_step", "goal": f"追加 {program} 这一步", "after": [],
        "action": {"tool": tool, "program": program},
        "effects": effects, "workdir_role": role, "expected_outputs": [],
    }]
    return route


def _route_versions(state) -> int:
    return len(state.artifact_versions(execution_route._canonical_route_artifact_id(state)))


def test_in_progress_route_refusal_shares_declares_preflight(tmp_path):
    """A real in_progress route (external job submitted, not finalized), no monkeypatch."""
    # Control: on the same fixture with the route merely declared (actionable), the
    # unpack call does get an amendment draft and the copy promise. Without this, a
    # missing draft below could be blamed on the schema instead of the preflight.
    control = _classified_state(tmp_path / "control")
    declared = asyncio.run(execution_route._declare_execution_route(
        control, route=_external_route()))
    assert declared["status"] == "success", declared
    control_block = _effectful_refusal(control)
    assert "amendment_reason" in str(control_block.get("next_action")), control_block
    assert "照抄" in control_block["error"]

    state = _classified_state(tmp_path / "live")
    _submitted_route(state)
    assert execution_route.build_route_snapshot(state)["route_state"] == "in_progress"

    block = _effectful_refusal(state)
    _assert_no_draft_and_no_copy_promise(block)
    # 048 v4：扣下草稿时附的是 declare 此刻会给的那份拒绝（同一 error_code、同一文案）
    assert block["route_declare_preflight"]["error_code"] == "route_active_attempt_reconciliation_required"
    # 048 v5：出口逐字写出实参（原来快照没带 scheduler/job_id，那段"逐字调用"从未成立）
    assert 'finalize_external_job(scheduler="slurm", job_id="31415"' in block["error"]

    # 同源：the amendment the draft would have handed out is refused by declare with
    # exactly the code the preflight guards — and the walk wrote nothing.
    amended = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_amended_with_step(
            state, program="tar", effects=["process_tree", "workspace_write"],
            role="run_root"),
        amendment_reason="按拒绝文案追加解包步骤",
    ))
    assert amended["error_code"] == "route_active_attempt_reconciliation_required", amended
    assert amended["error"] in block["error"]          # 同一段文案
    assert execution_route.build_route_snapshot(state)["route_state"] == "in_progress"
    assert _route_versions(state) == 1


def _seal_operation_closure(state) -> None:
    """Run one route-backed, censused local step that produces ``unpack.log``, and
    close the operation on that product: the run's operation closure is then sealed
    (complete).

    P0a v4（冻结批接入）：ROC(build) 要求满足义务的 route-backed 动作（census）且声明
    的产物出自该 attempt 收尾时冻结的收据——所以走 `_mechanical_execution` 的生产 API
    夹具：步骤声明 expected_outputs、产物在 begin/finish 之间写出。"""
    from nodes.experiment.tests._mechanical_execution import (
        record_completed_local_mechanical_action,
    )

    evidence = state.root / "unpack.log"
    record_completed_local_mechanical_action(
        state,
        step_id="execute",
        program="tar",
        produce=lambda: evidence.write_text("returncode=0\n", encoding="utf-8"),
        expected_outputs=["unpack.log"],
    )
    assert execution_route.build_route_snapshot(state)["route_state"] == "complete"
    closed = asyncio.run(operation_completion._record_operation_completion(
        state, task_kind="build", objective="解包并核对源码包", outcome="success",
        checks=[{"name": "rc", "passed": True, "evidence": {"returncode": 0}}],
        artifact_paths=[str(evidence)],
    ))
    assert closed["status"] == "success", closed
    status = operation_completion.operation_closure_status(state)
    assert status["sealed"] is True and status["kind"] == "complete", status


def test_sealed_closure_refusal_shares_declares_preflight(tmp_path):
    """A run whose operation closure is sealed: no draft, no promise, and the
    amendment is refused by declare with execution_route_sealed_by_operation_closure."""
    state = _classified_state(tmp_path)
    state.hook_state["_request_mode"] = "operation"
    _seal_operation_closure(state)

    block = _first_refusal(state)  # the git probe matches no step of the unpack route
    _assert_no_draft_and_no_copy_promise(block)
    assert block["route_declare_preflight"]["error_code"] == "execution_route_sealed_by_operation_closure"

    amended = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_amended_with_step(
            state, program="git", effects=["workspace_write"],
            role="managed_source_root"),
        amendment_reason="闭环后按拒绝文案追加一步",
    ))
    assert amended["error_code"] == "execution_route_sealed_by_operation_closure", amended
    assert _route_versions(state) == 1


# ── 048 v4（Codex 复审 08 号）：enforce 档下的 scientific 草稿 ─────────────────

_GATE_ENV = "EXPERIMENT_ENVELOPE_GATE"
_SOLVER = {
    "tool": "submit_job", "program": "./solver", "read_only": False, "dry_run": False,
    "observed_effects": ["external_job", "process_tree", "scientific_execution",
                         "workspace_write"],
    "workdir_roles": ["run_root"],
}


def _scientific_state(tmp_path):
    """A run bound to a frozen preregistration and classified scientific."""
    from core.state import State
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    saved = state.save_artifact(
        "pre_registration", "formal_solve", "# formal solve\n",
        metadata={"run_role": "primary", "execution_mode": "scientific",
                  "stage": "simulation", "expected_params": {"grid": 12}})
    state.mark_frozen(saved["id"])
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": saved["id"],
        "experiment_focus": "Run the preregistered formal solve.",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state, scope="scientific", reason="048 v4 envelope fixture"))
    assert classified["status"] == "success", classified
    return state


def _solver_route() -> dict:
    """The route the draft would propose for _SOLVER (placeholders already filled)."""
    return {
        "schema_version": 2, "goal": "formal solve", "evidence_refs": ["test:048-v4"],
        "steps": [{
            "id": "solver", "goal": "run the formal solver", "after": [],
            "action": {"tool": "submit_job", "program": "./solver"},
            "effects": ["external_job", "process_tree", "scientific_execution",
                        "workspace_write"],
            "workdir_role": "run_root", "expected_outputs": [],
        }],
    }


def test_scientific_enforce_refusal_withholds_draft_and_names_the_envelope_exit(
    tmp_path, monkeypatch,
):
    """Real scientific run, envelope gate enforce, first submit_job: no draft, no copy
    promise; the attached exit is declare's own execution_envelope_required text, and
    declaring the would-be draft indeed returns that code (同源)."""
    monkeypatch.setenv(_GATE_ENV, "enforce")
    state = _scientific_state(tmp_path / "enforce")
    block = _effectful_refusal(state, _SOLVER)
    _assert_no_draft_and_no_copy_promise(block)
    preflight = block["route_declare_preflight"]
    assert preflight["error_code"] == "execution_envelope_required", block
    assert preflight["step_ids"] == ["solver"]
    for fragment in ("declare_execution_envelope", "evidence_bearing", "evidence_refs"):
        assert fragment in block["error"], fragment

    declared = asyncio.run(execution_route._declare_execution_route(state, route=_solver_route()))
    assert declared["error_code"] == "execution_envelope_required", declared
    assert declared["error"] in block["error"]          # 同一段文案，不是相似的两段
    assert execution_route.load_canonical_route(state).get("reason") == "route_not_declared"


def test_scientific_warn_mode_control_still_gets_a_walkable_draft(tmp_path, monkeypatch):
    """Same run under warn: the draft is attached and, placeholders filled, declares."""
    monkeypatch.setenv(_GATE_ENV, "warn")
    state = _scientific_state(tmp_path / "warn")
    block = _effectful_refusal(state, _SOLVER)
    draft = block["next_action"]
    assert "照抄" in block["error"]
    assert "route_declare_preflight" not in block
    route = deepcopy(draft["arguments"]["route"])
    route["goal"] = "formal solve"
    for step in route["steps"]:
        step["goal"] = "run the formal solver"
    declared = asyncio.run(execution_route._declare_execution_route(
        state, **{**draft["arguments"], "route": route}))
    assert declared["status"] == "success", declared


# ── 048 v5（对抗审查 09-21）：再漏的门、门序、占位符 ────────────────────────────

def test_scientific_step_copied_into_an_operation_run_is_withheld_with_declares_code(tmp_path):
    """A route frozen while the run was scientific, then the run is redeclared to
    operation: the draft would copy the scientific_execution step verbatim and declare
    refuses route_scope_effect_mismatch."""
    from core.state import State
    from nodes.experiment.tools.run_contract import _classify_experiment_scope, load_execution_mode_view

    # a typed-none run classified scientific freezes a scientific route, then is
    # redeclared operation (改道 is allowed for typed none; a bound prereg cannot flip).
    # Under P0a the route cannot be declared before classification, so classify first.
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Run the formal solve, then only build.",
        "prereg_assignment": {"kind": "none", "reason": "048 v5 scope fixture"},
    }
    classified = asyncio.run(_classify_experiment_scope(
        state, scope="scientific", reason="formal solve first"))
    assert classified["status"] == "success", classified
    declared = asyncio.run(execution_route._declare_execution_route(state, route=_solver_route()))
    assert declared["status"] == "success", declared
    redeclared = asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="toolchain_build", reason="改道：只做构建"))
    assert redeclared["status"] == "success", redeclared
    assert load_execution_mode_view(state).get("mode") == "operational"

    block = _effectful_refusal(state)                                  # unmatched tar step
    _assert_no_draft_and_no_copy_promise(block)
    assert block["route_declare_preflight"]["error_code"] == "route_scope_effect_mismatch"
    assert block["route_declare_preflight"]["step_ids"] == ["solver"]
    amended = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_amended_with_step(state, program="tar", effects=["process_tree", "workspace_write"],
                                 role="run_root"),
        amendment_reason="改道后追加解包步骤",
    ))
    assert amended["error_code"] == "route_scope_effect_mismatch", amended
    assert amended["error"] in block["error"]


def test_corrupted_canonical_route_is_withheld_with_declares_integrity_code(tmp_path):
    state = _state(tmp_path)
    _freeze_unrelated_route(state)
    route_id = execution_route._canonical_route_artifact_id(state)
    path = state.find_artifact_path(route_id)
    assert path is not None
    with open(path, "a", encoding="utf-8") as handle:                # one byte, ledger sha256 no longer matches
        handle.write("#")
    assert execution_route.load_canonical_route(state)["status"] == "invalid"

    block = _first_refusal(state)
    _assert_no_draft_and_no_copy_promise(block)
    assert block["route_declare_preflight"]["error_code"] == "canonical_route_record_integrity_failed"
    assert "不要重新声明" in block["error"]
    declared = asyncio.run(execution_route._declare_execution_route(
        state, route=_single_step_route(tool="safe_run_bash", program="git", role="managed_source_root",
                                        effects=["workspace_write"])))
    assert declared["error_code"] == "canonical_route_record_integrity_failed", declared


def test_withheld_code_is_declares_first_refusal_when_two_gates_fail(tmp_path, monkeypatch):
    """in_progress external route + a scientific action under enforce: two gates fail;
    the withheld refusal must be the one declare returns first (same order, same code)."""
    state = _scientific_state(tmp_path)
    _submitted_route(state, _external_route())
    assert execution_route.build_route_snapshot(state)["route_state"] == "in_progress"
    monkeypatch.setenv(_GATE_ENV, "enforce")
    analyze = {**_SOLVER, "program": "./analyze"}
    block = _effectful_refusal(state, analyze)
    _assert_no_draft_and_no_copy_promise(block)
    withheld = block["route_declare_preflight"]["error_code"]
    amended = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_amended_with_step(state, program="./analyze", effects=_SOLVER["observed_effects"],
                                 role="run_root", tool="submit_job"),
        amendment_reason="追加分析步骤",
    ))
    assert amended["status"] == "error"
    assert withheld == amended["error_code"], (withheld, amended["error_code"])
    assert withheld in {"execution_envelope_required", "route_active_attempt_reconciliation_required"}


def test_placeholder_amendment_reason_is_refused_as_the_text_promises(tmp_path):
    state = _state(tmp_path)
    _freeze_unrelated_route(state)
    block = _first_refusal(state)
    draft = block["next_action"]
    assert "route_amendment_reason_required" in block["error"]
    route = deepcopy(draft["arguments"]["route"])
    route["goal"] = "真实目标"
    for step in route["steps"]:
        step["goal"] = "真实步骤目标"
    verbatim = asyncio.run(execution_route._declare_execution_route(
        state, **{**draft["arguments"], "route": route}))          # amendment_reason left as the placeholder
    assert verbatim["error_code"] == "route_amendment_reason_required", verbatim
