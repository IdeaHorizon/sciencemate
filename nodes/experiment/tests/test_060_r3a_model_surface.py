"""R3-A: test the Experiment surface the model actually receives.

The v3 protocol may retain legacy implementation code for old runs, but its
whitelist, rendered rules, loaded skills/assets and machine next-actions must
not teach the model to operate that control plane.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

from core.bootstrap import bootstrap
from core.loader import load_harness
from core.skill_registry import render_skills
from core.state import State
from core.tool_registry import _REGISTRY
from nodes.experiment import hooks
from nodes.experiment.tools import ledger
from nodes.experiment.tools import resource_manager as rm
from shared.tools.library.skill_tools import _load_skill


RETIRED_MODEL_PROTOCOL = re.compile(
    r"declared_route|route_step_id|route receipt|route step|expected_outputs"
    r"|declare_execution_envelope|verify_external_job_execution"
    r"|finalize_external_job|record_operation_completion|recovery_basis",
    re.I,
)


def _assert_natural(surface: str, source: str) -> None:
    hit = RETIRED_MODEL_PROTOCOL.search(surface)
    assert hit is None, f"{source}: retired model protocol {hit.group(0)!r}"


def _loaded_skill_text(payload: dict) -> str:
    return "\n".join(
        str(payload.get(key) or "")
        for key in ("description", "applies_when", "tools_used", "expected_outcome", "body_markdown", "content")
    )


def _v3_state(tmp_path: Path) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="p",
        session_id="owner",
    )
    state.hook_state["node_inputs"] = {"requested_work": "surface probe"}
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    return state


def test_real_whitelist_rules_and_tool_schemas_do_not_reintroduce_the_retired_protocol() -> None:
    bootstrap(force=True)
    harness = load_harness("experiment")
    _assert_natural(harness.system_prompt + "\n" + "\n".join(harness.rules), "active rules")

    assert "finish" in harness.tools
    for name in harness.tools:
        definition = _REGISTRY.tools[name]
        rendered = (definition.description or "") + "\n" + json.dumps(
            definition.parameters_schema or {}, ensure_ascii=False, sort_keys=True)
        _assert_natural(rendered, f"tool:{name}")

    # The shared artifact tools stay available for non-result artifacts, but
    # node registrations must not turn them into a manual v3 closure tutorial.
    freeze_text = _REGISTRY.tools["freeze_artifact"].description or ""
    # Previously all result contracts were hidden, including guidance that
    # would prevent the live model's mistaken manual save/freeze calls. Keep
    # the entry guidance, not a legacy schema or another form to fill out.
    from nodes.experiment.tools.delivery_transaction import MODEL_DELIVERY_CONTRACT
    from shared.tools.library.artifacts_extra import FREEZE_GATE_CONTRACTS
    for kind in ("clean_results", "raw_results"):
        assert FREEZE_GATE_CONTRACTS[kind] == MODEL_DELIVERY_CONTRACT
    assert MODEL_DELIVERY_CONTRACT["experiment_v3_delivery"] in freeze_text


def test_actual_indexed_and_conditional_skills_and_assets_keep_the_natural_loop(
    tmp_path: Path, monkeypatch,
) -> None:
    bootstrap(force=True)
    harness = load_harness("experiment")
    index = render_skills(list(harness.skills or []), node_type="experiment", mode="index")
    _assert_natural(index, "skill index")

    state = State.new("experiment", tmp_path / "skill-run")
    for name in [*list(harness.skills or []), "gpu-hpc-porting"]:
        loaded = asyncio.run(_load_skill(state, name))
        assert loaded["status"] == "success", (name, loaded)
        _assert_natural(_loaded_skill_text(loaded), f"skill:{name}")

    feasibility = asyncio.run(_load_skill(state, "feasibility-ladder"))
    text = _loaded_skill_text(feasibility)
    assert 'finish(outcome="blocked"' in text
    assert "save_artifact" not in text

    scientific = asyncio.run(_load_skill(state, "scientific-results"))
    assert scientific["status"] == "success", scientific
    assert scientific["skill_status"] == "proposed"
    text = _loaded_skill_text(scientific)
    assert "exploratory, no prereg" in text
    assert "不证明程序实际使用" in text
    assert "scientific_params" in text and "runtime_params" in text
    assert "live LLM、UI 或受管调度器路径已经验收" in text

    retired = asyncio.run(_load_skill(state, "primary-scientific-closure"))
    assert retired["status"] == "error", retired

    assets = [
        ("scheduler-longrun", asset)
        for asset in hooks._SCHEDULER_FACTS_ASSETS.values()
    ]
    assets.append(("hpc-build", "references/parallel-performance.md"))
    assets.append(("ai-workloads", "references/checkpoint-resume.md"))
    loaded_assets = {}
    for name, asset in assets:
        loaded = asyncio.run(_load_skill(state, name, asset=asset))
        assert loaded["status"] == "success", (name, asset, loaded)
        _assert_natural(str(loaded.get("content") or ""), f"{name} asset:{asset}")
        loaded_assets[(name, asset)] = loaded

    resume = loaded_assets[("ai-workloads", "references/checkpoint-resume.md")]
    assert resume["truncated"] is False
    resume_text = str(resume["content"])
    for required in (
        "持久化文件",
        "fresh process",
        "format/schema",
        "配置身份",
        "下一批数据",
        "下一次更新",
        "warm start",
        "绝对与相对容差",
    ):
        assert required in resume_text
    assert "纯推理不要求训练状态" in resume_text
    assert "loss、模型输出或权重/状态增量" in resume_text

    cuda = State.new("experiment", tmp_path / "cuda-run")
    monkeypatch.setattr(hooks, "_gpu_backend_hint", lambda _state: "nvidia_cuda")
    messages = hooks._gpu_skill_injector_on_turn_start(
        SimpleNamespace(state=cuda, turn=1)) or []
    assert messages
    _assert_natural("\n".join(message.content for message in messages), "conditional GPU routing")


def test_scheduler_asset_router_observes_every_active_terminal_reader() -> None:
    for tool in ("job_status", "check_external_job_health", "wait_for_external_job"):
        health = {
            "scheduler": "slurm",
            "job_id": "31415",
            "scheduler_phase": "terminal",
        }
        result = {"health": health} if tool == "wait_for_external_job" else health
        observed = hooks._terminal_scheduler_observations([
            {"name": tool, "args": {"scheduler": "slurm", "job_id": "31415"}, "result": result},
        ])
        assert observed == [("slurm", "31415")], (tool, observed)


def test_representative_v3_next_actions_and_cancel_results_are_natural(tmp_path: Path) -> None:
    state = _v3_state(tmp_path)
    record = {
        "scheduler": "slurm",
        "job_id": "9001",
        "submission_nonce": "n1",
    }
    surfaces = {
        "cancel already ended": rm._cancel_already_ended_response(
            record,
            lifecycle_status="open",
            observed={"state": "FAILED", "exit_code": 2},
        ),
        "cancel state unknown": rm._external_job_state_unknown_exit(
            record,
            source="cancel_job",
            observation={
                "kind": "scheduler_reports_job_unknown",
                "stderr": "Invalid job id specified",
            },
        ),
        "cancel confirmed": rm._cancel_success_response(
            state,
            record,
            lifecycle={"lifecycle_status": "cancelled"},
            cancel_result={"ok": True},
            evidence_artifact_id="cancel-intent-1",
            route_projection={"status": "success", "reason": "legacy"},
            task_completion={"status": "success"},
            cleanup={"status": "success"},
            idempotent=False,
        ),
    }
    for name, payload in surfaces.items():
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        _assert_natural(rendered, name)
        assert "finish" in rendered


def test_preflight_advice_points_to_finish_not_a_hand_written_result_log(
    tmp_path: Path, monkeypatch,
) -> None:
    state = _v3_state(tmp_path)
    monkeypatch.setattr(
        rm,
        "_recommend",
        lambda *_args, **_kwargs: {
            "resource_profile": {"core_platform_capabilities": {"host_software": []}},
            "recommendation": {"warnings": ["capacity is tight"]},
        },
    )
    result = asyncio.run(rm._preflight_build_resources(
        state,
        compile_mode="serial",
        mpi_ranks=1,
        cpus_per_rank=1,
        gpus=0,
        memory_gb=1.0,
        walltime_minutes=5,
        runtime_resource_policy="fixed",
    ))
    assert result["status"] == "success"
    assert "finish" in result["recommendation"]
    assert "experiment_log" not in result["recommendation"]
