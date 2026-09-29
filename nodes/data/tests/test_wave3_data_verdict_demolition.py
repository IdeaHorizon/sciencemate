"""判决拆除第三波 · data 域（plan_E，2026-09-02）。

每条降格一个测试：从前会被拒的输入现在照走，且如实标记落在记录里 ——
墙加回去必转红。schema 类各一条：派发口按 schema 拒绝、工具体不再二审。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import zipfile
from pathlib import Path

from core.state import State


def _state(node_type: str = "data") -> State:
    return State.new(node_type=node_type, base_dir=Path(tempfile.mkdtemp()))


def _events(state: State) -> list[dict]:
    path = state.transcript_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(coro):
    return asyncio.run(coro)


# ── schema：契约只在注册 schema 声明一次，派发口核一次 ─────────────────────

def _dispatch(tool: str, **kw):
    from core.bootstrap import bootstrap
    from core.tool_registry import execute

    bootstrap()
    return _run(execute(tool, _state(), **kw))


def test_recover_atomic_structure_bad_operation_is_rejected_at_dispatch_with_legal_values():
    r = _dispatch("recover_atomic_structure", structure_name="Cu", operation="bogus")
    assert r["status"] == "error" and r.get("parameter_violations")
    assert "assess" in r["error"] and "finalize_blocked" in r["error"]


def test_prepare_scientific_mesh_bad_operation_is_rejected_at_dispatch():
    r = _dispatch("prepare_scientific_mesh", spec="x", operation="bogus")
    assert r["status"] == "error" and r.get("parameter_violations")
    assert "build_profile" in r["error"]


def test_execute_preprocessing_plan_bad_plan_kind_is_rejected_at_dispatch():
    r = _dispatch("execute_preprocessing_plan", plan_kind="bogus")
    assert r["status"] == "error" and r.get("parameter_violations")
    assert "reference_evidence_only" in r["error"]


def test_data_web_search_blank_query_is_rejected_at_dispatch_not_as_provider_error():
    r = _dispatch("data_web_search", query="   ")
    assert r["status"] == "error" and r.get("parameter_violations")
    assert "query" in r["error"]


# ── merge：zip-slip 只在 resolve() 包含性检查一处判 ─────────────────────────

def test_zip_member_escaping_extraction_dir_is_still_rejected_after_merge():
    from nodes.data.tools.web_search import _extract_mesh_archive

    root = Path(tempfile.mkdtemp())
    for name in ("../evil.txt", "/abs/evil.txt"):
        archive = root / f"a{abs(hash(name))}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(name, "x")
        r = _extract_mesh_archive(archive)
        assert r["status"] == "error" and "escapes extraction directory" in r["error"], name
        assert not (root / "evil.txt").exists()
    benign = root / "ok.zip"
    with zipfile.ZipFile(benign, "w") as bundle:
        bundle.writestr("case/a/../mesh.msh", "$MeshFormat\n")
    r = _extract_mesh_archive(benign)
    assert r["status"] != "error"


# ── downgrade：mesh_generator 6057 原始 CAD 照剖，挂 geometry_representation_unknown ──

async def _fake_worker(state, generator, **params):
    return {"status": "success", "mesh_file": str(Path(params["case_dir"]) / "mesh.msh"), "source_trace": []}


def test_raw_cad_is_meshed_and_carries_geometry_representation_unknown(monkeypatch):
    from nodes.data.tools import mesh_generator

    monkeypatch.setattr(mesh_generator, "_run_mesh_worker", _fake_worker)
    state = _state()
    stl = state.root / "body.stl"
    stl.write_text("solid body\nendsolid body\n", encoding="utf-8")

    r = _run(mesh_generator._generate_computational_mesh(
        state, discipline="cfd", mesh_type="geometry_file_gmsh",
        geometry="mesh the provided STL body",
        parameters=json.dumps({"geometry_file": str(stl)}),
    ))
    assert r["status"] == "success", r
    assert r["geometry_representation_unknown"] is True
    assert r["deliverable_valid"] is False
    assert "geometry_representation_unknown" in r["delivery_blocked_by"]
    assert r["obligations"][0]["kind"] == "geometry_representation_unknown"
    assert any(e["event"] == "mesh_geometry_representation_unknown" for e in _events(state))

    declared = _run(mesh_generator._generate_computational_mesh(
        state, discipline="cfd", mesh_type="geometry_file_gmsh",
        geometry="mesh the provided STL body",
        parameters=json.dumps({
            "geometry_file": str(stl),
            "geometry_representation": "fluid_domain",
            "computational_domain_complete": True,
        }),
    ))
    assert declared["status"] == "success"
    assert "geometry_representation_unknown" not in declared
    assert declared.get("deliverable_valid") is not False


# ── mesh_iteration_advisor：公开 benchmark 义务随 case profile 目录一起退场 ──
#
# 原判据 test_public_benchmark_without_reference_notes_resolves_with_obligation
# 断言「像公开 benchmark 且没写 reference_notes → 照走 + 挂
# public_benchmark_params_unverified 义务」。#909 删掉了
# nodes/data/cases/cfd/*.yaml（router 改成由调用方/模型选注册过的 mesh_type，
# 不再驮一本算例目录），_resolve_mesh_iteration_inputs 里 public_case 随之被
# 硬钉成 None —— 那条义务永远挂不上。
#
# 这不是丢了保证。义务的**前提**是「标准域尺寸/边界条件取自本地 profile 与公开
# 参考默认值，未经检索核实」；现在一个未经核实的值都不再填，前提本身没了，
# 义务是空的而不是被删的。
#
# 下面这条判据钉住的正是那个前提，而不是「public_case 是不是 None」这种写法：
# 只要 resolve 不挂义务，它就必须一个未经核实的值都没填。哪天又开始填了 ——
# 不管是 public_case 复活还是换了别的来源 —— 这条转红，提醒把义务一起恢复。

def test_no_obligation_only_because_nothing_unverified_was_filled_in():
    from nodes.data.tools.mesh_iteration_advisor import _resolve_mesh_iteration_inputs

    state = _state()
    r = _run(_resolve_mesh_iteration_inputs(
        state, spec="lid driven cavity benchmark mesh", parameters="{}",
        require_user_confirmation=False,
    ))
    assert r["status"] == "success", r

    if r.get("obligations"):
        return  # 挂了义务 = 如实标记了，本判据不管挂的是哪一条

    # 没挂义务，那就必须什么都没替用户定：只允许「这是哪类网格」这一层路由结论，
    # 域尺寸 / 边界条件 / 雷诺数这些必须来自用户或 reference_notes。
    routing_only = {"case_type", "mesh_type"}
    filled = set(r.get("resolved_parameters") or {}) - routing_only
    assert not filled, (
        f"没挂任何义务却替用户填了 {sorted(filled)} —— 这些值的出处是什么？"
        "取自未经检索核实的默认值就必须挂 public_benchmark_params_unverified "
        "一类的义务（判据拆除战役 RFC §4：降格成义务，不是静默填上）。"
    )


# ── downgrade：preprocessing_planner 7100/7290/7134/7153/7268 ────────────────

def _analysis(**scope) -> dict:
    return {
        "task_scope": {
            "allowed_capabilities": ["configuration_generation"],
            "excluded_capabilities": [],
            "caller_scope_locked": False,
            **scope,
        },
        "required_files": [],
        "evidence": [{"source_type": "planning", "detail": "unit"}],
    }


def _request(**overrides) -> dict:
    return {
        "id": "r1", "query": "solver manual boundary conditions", "tool": "data_web_search",
        "web_search_allowed": True, "asset_kind": "reference",
        # 非外部能力（不在 EXTERNAL_WORKFLOW_CAPABILITIES）且不在 allowed 里：
        # 这是 Analyst 推导 scope 会判越界的那一档。
        "workflow_capability": "preprocessing_script_generation", "missing": "boundary semantics",
        **overrides,
    }


def test_reference_scope_deviation_is_recorded_and_the_request_proceeds():
    from nodes.data.tools.preprocessing_planner import _scope_allows, _search_response_for_gaps

    state = _state()
    analysis = _analysis()
    assert not _scope_allows(analysis["task_scope"], "preprocessing_script_generation",
                             analysis=analysis, reference=True)
    r = _search_response_for_gaps(state, analysis, "unit", requests=[_request()])
    assert r is not None and r["status"] == "needs_reference_search", r
    deviations = analysis["routing_audit"]["scope_deviations"]
    assert deviations and deviations[0]["capability"] == "preprocessing_script_generation"
    assert any(e["event"] == "preprocessing_reference_scope_deviation" for e in _events(state))


def test_caller_locked_capability_is_still_authoritative():
    from nodes.data.tools.preprocessing_planner import _search_response_for_gaps

    analysis = _analysis(
        allowed_capabilities=[], excluded_capabilities=["geometry_acquisition"], caller_scope_locked=True,
    )
    r = _search_response_for_gaps(
        _state(), analysis, "unit",
        requests=[_request(workflow_capability="geometry_acquisition", asset_kind="geometry_or_mesh")],
    )
    assert r["status"] == "needs_revision" and r["stop_reason"] == "capability_locked_out_by_caller"
    assert not (analysis.get("routing_audit") or {}).get("scope_deviations")


def test_reference_execution_error_is_retried_and_only_the_same_signature_trips_the_breaker():
    from nodes.data.planning.store import PlanningStore
    from nodes.data.tools.preprocessing_planner import _search_response_for_gaps

    state = _state()
    store = PlanningStore(state)
    analysis = _analysis(allowed_capabilities=["geometry_acquisition"])
    req = _request(gap_id="gap1", workflow_capability="geometry_acquisition", asset_kind="geometry_or_mesh")

    def fail(error: str) -> None:
        store.record_reference_outcome("gap1", status="execution_error", fields={"error": error})

    fail("HTTP 503")
    r1 = _search_response_for_gaps(state, analysis, "unit", requests=[req])
    assert r1["status"] == "needs_reference_search", r1
    assert store.load_reference_state()["gaps"]["gap1"]["retry_count"] == 1

    fail("HTTP 503")
    r2 = _search_response_for_gaps(state, analysis, "unit", requests=[req])
    assert r2["status"] == "needs_reference_search"
    assert store.load_reference_state()["gaps"]["gap1"]["retry_count"] == 2

    fail("HTTP 503")
    r3 = _search_response_for_gaps(state, analysis, "unit", requests=[req])
    assert r3["status"] == "externally_blocked" and r3["stop_reason"] == "reference_retry_exhausted"
    assert "出口" in r3["error"] and "infeasible" in r3["error"]

    fail("HTTP 404 not found")          # 换了错误 = 换了签名，计数归零
    r4 = _search_response_for_gaps(state, analysis, "unit", requests=[req])
    assert r4["status"] == "needs_reference_search"
    assert store.load_reference_state()["gaps"]["gap1"]["retry_count"] == 1
    assert sum(e["event"] == "preprocessing_reference_retry" for e in _events(state)) == 3


def test_no_executable_reference_step_proceeds_with_unverified_marker():
    from nodes.data.tools.preprocessing_planner import _search_response_for_gaps

    state = _state()
    analysis = _analysis()
    r = _search_response_for_gaps(
        state, analysis, "unit", requests=[_request(web_search_allowed=False)],
    )
    assert r is None
    marks = analysis["reference_evidence_unverified"]
    assert marks and marks[0]["request_id"] == "r1"
    assert any("unverified" in note for note in analysis["contract_review_notes"])
    assert any(e["event"] == "preprocessing_reference_evidence_unverified" for e in _events(state))


def test_all_verified_requests_proceed_without_an_unverified_marker():
    from nodes.data.planning.store import PlanningStore
    from nodes.data.tools.preprocessing_planner import _search_response_for_gaps

    state = _state()
    PlanningStore(state).record_reference_outcome("gap1", status="downloaded_verified")
    analysis = _analysis(allowed_capabilities=["geometry_acquisition"])
    r = _search_response_for_gaps(
        state, analysis, "unit",
        requests=[_request(gap_id="gap1", workflow_capability="geometry_acquisition", asset_kind="geometry_or_mesh")],
    )
    assert r is None
    assert "reference_evidence_unverified" not in analysis


def test_route_deviation_from_local_geometry_to_acquisition_is_declared_not_rejected():
    from nodes.data.tools.preprocessing_planner import reference_plan_from_generation_transition

    state = _state()
    analysis = _analysis(
        allowed_capabilities=["mesh_generation", "geometry_acquisition"],
        declared_operations=["generate the geometry from coordinates"],
    )
    analysis["required_files"] = [{
        "id": "domain_mesh", "asset_role": "computational_mesh", "representation": "mesh",
        "source_strategy": "local_generation", "acquisition_kind": "generated_artifact",
    }]
    plan = {"requirement_analysis": analysis}
    execution = {
        "failed_step": "mesh_1",
        "transition": {
            "tool_name": "prepare_scientific_mesh",
            "missing_fields": [],
            "reference_request": _request(
                id="geo", query="naca 0012 coordinates", asset_kind="geometry_or_mesh",
                workflow_capability="geometry_acquisition",
            ),
        },
    }
    r = reference_plan_from_generation_transition(state, plan, execution)
    assert r is not None and r.get("reason") != "reference_route_conflicts_with_local_generation_contract", r
    assert r["status"] == "needs_reference_search"
    deviations = analysis["routing_audit"]["route_deviations"]
    assert deviations and deviations[0]["chosen_route"] == "geometry_acquisition"
    assert any(e["event"] == "preprocessing_reference_route_deviation" for e in _events(state))
