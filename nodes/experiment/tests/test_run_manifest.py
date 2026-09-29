"""Tests for the lightweight per-run provenance record."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from pathlib import Path

from core.state import State

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import repro_snapshot, run_contract
from nodes.experiment import hooks


def _state(tmp_path):
    return State.new("experiment", tmp_path)


def _save_frozen(state, artifact_type, name, content, metadata=None):
    """save + mark_frozen：冻结的事实只出自账本的 freeze 行，save 行里的 frozen 键
    会被剥掉（core.ledger.FREEZE_OWNED_METADATA）。返回 save_artifact 的结果。"""
    saved = state.save_artifact(artifact_type, name, content, metadata=metadata)
    state.mark_frozen(saved["id"])
    return saved


def _bind_operation_inputs(state) -> None:
    state.hook_state.setdefault("node_inputs", {
        "experiment_focus": "Execute the declared operation fixture and retain its evidence.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This operation fixture consumes no project preregistration.",
        },
    })


def test_scope_tool_description_uses_claim_boundary_not_workload_shape():
    """Classification follows the purpose of the evidence, not job shape."""
    from core.tool_registry import get_tool

    description = get_tool("classify_experiment_scope").description

    assert "mechanical acceptance criteria" in description
    assert "evaluate a scientific claim" in description
    assert "replication" in description
    normalized = description.lower()
    assert "simulation, training, parameter scans, repeated timing" in normalized
    assert "do not by themselves determine the scope" in normalized
    assert "scientific covers simulation" not in description
    assert "parameter scans, analysable results" not in description
    assert "a bound frozen preregistration makes the run scientific" in normalized
    assert "the assignment remains pending" in normalized
    assert "candidate observations and are never automatically bound" in normalized


def _complete_operation(state, *, task_kind: str = "generic", checks=None, outcome: str = "success", next_step: str = ""):
    from nodes.experiment.tools.operation_completion import _record_operation_completion
    if checks is None:
        checks = [{"name": "operation_check", "passed": outcome == "success",
                   "evidence": {"returncode": 0 if outcome == "success" else 1}}]
    evidence = Path(state.root) / "operation_test.stdout"
    evidence.write_text("returncode=" + ("0" if outcome == "success" else "1") + "\n", encoding="utf-8")
    return asyncio.run(_record_operation_completion(
        state, task_kind=task_kind, objective="verify an operational task",
        outcome=outcome, checks=checks, artifact_paths=[str(evidence)], next_step=next_step,
    ))


def test_manifest_defaults_to_non_analysis_secondary(tmp_path):
    state = _state(tmp_path)
    state.transcript_path.write_text('{"event":"run_started"}\n', encoding="utf-8")
    log_dir = state.root / "outputs" / "experiment" / "runtime" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "0001_deadbeef.log").write_text(
        "# returncode: 1\nerror\n", encoding="utf-8")

    manifest = run_contract.create_run_manifest(state, status="running")

    assert manifest["run_role"] == "secondary"
    assert manifest["run_authority_identity_status"] == "unbound"
    assert manifest["analysis_eligible"] is False
    assert manifest["status"] == "running"
    assert manifest["return_code"] == 1
    assert manifest["logs"][0]["path"] == \
        "outputs/experiment/runtime/logs/0001_deadbeef.log"
    assert (
        state.root / "outputs" / "experiment" / "repro" / "run_manifest.json"
    ).is_file()
    # 每个 run 一份身份（run_manifest_<run_id>）：账本登记、正文是原生文件。
    manifests = state.list_artifacts("run_manifest")
    assert [item["name"] for item in manifests] == [f"run_manifest_{state.run_id}"]
    record = state.read_artifact(manifests[0]["id"])
    assert json.loads(record["content"])["run_id"] == state.run_id
    assert state.find_artifact_path(manifests[0]["id"]).is_file()


def test_manifest_skips_log_outside_state_root(tmp_path):
    state = _state(tmp_path)
    external_logs = tmp_path.parent / "external-runtime" / "logs"
    external_logs.mkdir(parents=True)
    (external_logs / "0001_external.log").write_text("# returncode: 0\nok\n", encoding="utf-8")

    original = run_contract._existing_logs_dir
    run_contract._existing_logs_dir = lambda _state: external_logs
    try:
        manifest = run_contract.create_run_manifest(state, status="completed")
    finally:
        run_contract._existing_logs_dir = original

    assert manifest["logs"] == []


def test_manifest_uses_structured_preregistration_contract(tmp_path):
    state = _state(tmp_path)
    prereg = _save_frozen(
        state,
        "pre_registration",
        "contract",
        "## frozen prereg\n",
        metadata={
            "run_role": "primary",
            "analysis_eligible": True,
            "experiment_id": "exp-01",
            "protocol_version": "protocol-01",
            "dataset_id": "dataset-01",
            "scope_manifest_id": "scope-01",
            "protocol_amendment_id": "amendment-01",
        },
    )
    state.hook_state["node_inputs"] = {"prereg_artifact_id": prereg["id"]}

    manifest = run_contract.create_run_manifest(state, status="completed")

    assert manifest["run_role"] == "primary"
    assert manifest["analysis_eligible"] is True
    assert manifest["experiment_id"] == "exp-01"
    assert manifest["scope_manifest_id"] == "scope-01"
    assert manifest["protocol_amendment_id"] == "amendment-01"


def test_manifest_persists_observed_execution_params_without_mutating_prereg(tmp_path):
    state = _state(tmp_path)
    _save_frozen(
        state, "pre_registration", "contract", "## frozen prereg\n",
        metadata={"expected_params": {"grid": [50, 50]}},
    )
    run_contract.record_actual_run_params(
        state, "safe_run_bash", {"grid": [50, 50]},
    )

    manifest = run_contract.create_run_manifest(state, status="completed")

    assert manifest["actual_run_params"] == {"grid": [50, 50]}
    assert manifest["actual_run_params_sources"] == ["safe_run_bash"]
    prereg = state.read_artifact(state.list_artifacts("pre_registration")[0]["id"])
    # 账本读时把 freeze 行折进 metadata（frozen + frozen_at）；除此之外一个键都不许多。
    prereg_metadata = dict(prereg["metadata"])
    assert prereg_metadata.pop("frozen_at")
    assert prereg_metadata == {"frozen": True, "expected_params": {"grid": [50, 50]}}


def test_manifest_reads_legacy_logs_without_writing_new_logs_there(tmp_path):
    state = _state(tmp_path)
    legacy = state.root / "logs"
    legacy.mkdir()
    (legacy / "0001_legacy.log").write_text(
        "# returncode: 7\nlegacy\n", encoding="utf-8")

    manifest = run_contract.create_run_manifest(state, status="completed")

    assert manifest["return_code"] == 7
    assert manifest["logs"][0]["path"] == "logs/0001_legacy.log"
    assert not (state.root / "outputs" / "experiment" / "runtime" / "logs").exists()


def test_force_creates_bundle_for_failed_primary(tmp_path, monkeypatch):
    state = _state(tmp_path)
    monkeypatch.setattr(repro_snapshot, "_run", lambda *args, **kwargs: "")

    bundle = repro_snapshot.create_repro_bundle(
        state,
        snap={},
        result_info={"credibility": "questionable"},
        force=True,
    )

    assert bundle is not None
    assert (
        state.root / "outputs" / "experiment" / "repro" / "manifest.json"
    ).is_file()
    assert bundle["min_run_success"] is False


def test_secondary_missing_log_gets_frozen_minimal_record(tmp_path):
    state = _state(tmp_path)

    asyncio.run(hooks._secondary_experiment_log_recovery_on_end(
        SimpleNamespace(state=state, turn=0),
        SimpleNamespace(status="failed"),
    ))

    records = state.list_artifacts("experiment_log")
    assert len(records) == 1
    record = state.read_artifact(records[0]["id"])
    assert record is not None
    assert record["metadata"]["auto_generated"] is True
    assert record["metadata"]["frozen"] is True
    assert "verdict: inconclusive" in record["content"]
    assert "credibility: invalid" in record["content"]


def test_primary_missing_log_gets_explicit_failed_record(tmp_path):
    state = _state(tmp_path)
    prereg = _save_frozen(
        state, "pre_registration", "contract", "## prereg\n",
        metadata={"run_role": "primary", "analysis_eligible": True},
    )
    state.hook_state["node_inputs"] = {"prereg_artifact_id": prereg["id"]}

    asyncio.run(hooks._secondary_experiment_log_recovery_on_end(
        SimpleNamespace(state=state, turn=0),
        SimpleNamespace(status="failed"),
    ))

    records = state.list_artifacts("experiment_log")
    assert len(records) == 1
    record = state.read_artifact(records[0]["id"])
    assert record is not None
    assert record["metadata"]["auto_generated"] is True
    # 恢复记录不再写资格位（它已从目标契约删除）；身份由 run_role 承担。
    assert "analysis_eligible" not in record["metadata"]
    assert record["metadata"]["run_role"] == "primary"
    assert "primary_run_missing_final_experiment_log" in record["content"]
    assert "credibility: invalid" in record["content"]


def test_recovery_record_captures_real_returncode_and_logs(tmp_path):
    """兜底记录必须真拿到 returncode 和日志路径。

    回归：此前读方自己拼 artifact 落盘路径，而写方的落盘名另有来源，文件永远
    不存在，两个字段恒为 unknown/空 —— 正好是这份记录唯一的用途。现在读方拿
    ``create_run_manifest`` 的返回值，不拼路径。
    """
    state = _state(tmp_path)
    log_dir = state.root / "outputs" / "experiment" / "runtime" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "0001_deadbeef.log").write_text(
        "# returncode: 7\nboom\n", encoding="utf-8")

    asyncio.run(hooks._secondary_experiment_log_recovery_on_end(
        SimpleNamespace(state=state, turn=0),
        SimpleNamespace(status="failed"),
    ))

    record = state.read_artifact(state.list_artifacts("experiment_log")[0]["id"])
    assert "returncode: 7" in record["content"]
    assert "outputs/experiment/runtime/logs/0001_deadbeef.log" in record["content"]
    assert "manifest_unavailable" not in record["content"]


def test_recovery_record_marks_manifest_unavailable_on_failure(tmp_path, monkeypatch):
    """拿不到 manifest 时必须显式标注，而不是留下看起来正常的空值。"""
    state = _state(tmp_path)
    monkeypatch.setattr(
        run_contract, "create_run_manifest",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    asyncio.run(hooks._secondary_experiment_log_recovery_on_end(
        SimpleNamespace(state=state, turn=0),
        SimpleNamespace(status="failed"),
    ))

    record = state.read_artifact(state.list_artifacts("experiment_log")[0]["id"])
    assert "manifest_unavailable: true" in record["content"]
    assert "returncode: unknown" in record["content"]


def test_auto_recovery_record_does_not_self_certify_quality_gates(tmp_path):
    """零工具调用的 run 不得靠框架自己写的兜底记录通过科研门。

    回归：recovery 写入 "verdict: inconclusive" 和 "未发现 methodological /
    dead_end finding"，contract_audit 再读同一段文本判定，两道 mechanical
    quality check 就会在 agent 什么都没做的情况下通过。
    """
    state = _state(tmp_path)

    asyncio.run(hooks._secondary_experiment_log_recovery_on_end(
        SimpleNamespace(state=state, turn=0),
        SimpleNamespace(status="failed"),
    ))
    hooks.experiment_contract_audit_on_end(
        SimpleNamespace(state=state, turn=0),
        SimpleNamespace(status="failed"),
    )

    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
    ]
    for event_name in ("experiment_verdict_audit", "experiment_sediment_audit"):
        latest = [e for e in events if e.get("event") == event_name][-1]
        assert latest["passed"] is False, event_name
        assert latest["auto_generated_record"] is True, event_name

    # The record is nevertheless complete enough to route recovery: it is
    # frozen, citation-audited and contains the mandatory terminal fields.
    terminal = [event for event in events
                if event.get("event") == "terminal_experiment_record_audit"][-1]
    assert terminal["applicable"] is True
    assert terminal["passed"] is True
    assert terminal["citation_validation_present"] is True
    assert terminal["n_phantom_claims"] == 0




def test_gpu_skill_is_conditional_and_injected_for_explicit_gpu_contract(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state.hook_state["run_contract"] = {
        "run_role": "secondary", "analysis_eligible": False, "requires_gpu": True,
        "expected_params": {"accelerator_backend": "cuda"},
    }

    monkeypatch.setattr("core.skill_registry.get_skill", lambda name: object())
    messages = hooks._gpu_skill_injector_on_turn_start(
        SimpleNamespace(state=state, turn=1),
    )

    assert messages is not None
    assert "load_skill(name='gpu-hpc-porting')" in messages[0].content
    assert "### Skill:" not in messages[0].content
    assert state.hook_state["_gpu_skill_injected"] is True
    assert hooks._gpu_skill_injector_on_turn_start(
        SimpleNamespace(state=state, turn=2),
    ) is None


def test_non_cuda_gpu_does_not_inject_cuda_skill(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state.hook_state["run_contract"] = {
        "run_role": "secondary", "analysis_eligible": False, "requires_gpu": True,
        "expected_params": {"accelerator_backend": "rocm"},
    }
    monkeypatch.setattr(
        "core.skill_registry.get_skill",
        lambda name: (_ for _ in ()).throw(AssertionError("must not load CUDA skill")),
    )

    messages = hooks._gpu_skill_injector_on_turn_start(
        SimpleNamespace(state=state, turn=1),
    )

    assert messages is not None
    assert "不要假定 CUDA" in messages[0].content
    assert '"event": "gpu_skill_not_applicable"' in state.transcript_path.read_text(
        encoding="utf-8",
    )




def test_actual_params_only_accept_structured_sources(tmp_path):
    """实际参数来自已执行命令的解析，不是 LLM 自述。"""
    from nodes.experiment.tools import safe_bash as sb

    state = _state(tmp_path)
    sb._record_mpi_actual_params(state, 'cd /w && mpirun -np 8 ./vasp_std')

    assert state.hook_state["actual_run_params"]["mpi_ranks"] == 8
    assert any("mpirun" in s for s in state.hook_state["actual_run_params_sources"])


def test_non_mpi_command_records_nothing(tmp_path):
    from nodes.experiment.tools import safe_bash as sb

    state = _state(tmp_path)
    sb._record_mpi_actual_params(state, "make -j8 all")

    assert "actual_run_params" not in state.hook_state



def test_experiment_classifies_operation_and_records_caller_provenance(tmp_path):
    state = _state(tmp_path)
    state.hook_state["node_inputs"] = {
        "stage": "diagnostic", "caller_node_type": "orchestrator",
        "caller_run_id": "parent-123",
    }

    _bind_operation_inputs(state)
    classified = asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="package_install",
        reason="Install h5py and verify that it imports successfully.",
    ))
    contract = run_contract.load_run_contract(state)
    manifest = run_contract.create_run_manifest(state, status="running")

    assert classified["status"] == "success"
    assert contract["execution_mode"] == "operational"
    assert contract["operation_kind"] == "package_install"
    assert contract["analysis_eligible"] is False
    assert contract["review_eligible"] is False
    assert contract["requires_hypothesis_verdict"] is False
    assert contract["run_authority_identity_status"] == "verified"
    assert manifest["execution_mode"] == "operational"
    assert manifest["run_authority_identity_status"] == "verified"
    assert manifest["invocation"]["return_target_node_type"] == "orchestrator"
    assert manifest["invocation"]["return_target_run_id"] == "parent-123"


def test_operation_scope_conflict_with_bound_primary_prereg_is_declared_not_refused(tmp_path):
    """判决拆除 O1（rc:683 降格，2026-08-31，专审一）。

    声明与冻结 prereg 冲突不再拒绝 —— 偏离申报进账（prereg_deviation_declared），
    而 contract 层的权威保护仍在：caller-bound primary prereg 下 execution_mode
    被机械保回 scientific 并留 warning（账本不因节点自分类而降级）。
    """
    state = _state(tmp_path)
    prereg_id = _save_frozen(
        state, "pre_registration", "formal", "# frozen prereg",
        metadata={"run_role": "primary", "expected_params": {"n": 2}},
    )["id"]
    state.hook_state["node_inputs"] = {"prereg_artifact_id": prereg_id}

    _bind_operation_inputs(state)
    classified = asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="package_install",
        reason="Attempt to bypass a caller-bound scientific preregistration.",
    ))
    contract = run_contract.load_run_contract(state)

    assert classified["status"] == "success", classified
    assert classified["classification"]["prereg_deviation"]["kind"] == \
        "requested_operation_with_governing_prereg"
    deviations = state.hook_state.get(run_contract.PREREG_DEVIATIONS_KEY) or []
    assert deviations and deviations[0]["kind"] == "requested_operation_with_governing_prereg"
    # 权威保护：任何 governing prereg 都把 effective contract 保在 scientific。
    assert contract["execution_mode"] == "scientific"
    assert contract["analysis_eligible"] is True


def test_bound_secondary_legacy_prereg_derives_scientific_scope(tmp_path):
    state = _state(tmp_path)
    prereg_id = _save_frozen(
        state, "pre_registration", "secondary_diagnostic", "# prereg\n## Research Question\nscientific threshold",
        metadata={"run_role": "secondary", "stage": "diagnostic"},
    )["id"]
    state.hook_state["node_inputs"] = {"prereg_artifact_id": prereg_id}

    _bind_operation_inputs(state)
    # 请求 operation 会被诚实记为偏离，但 governing prereg 在入口即决定
    # effective scientific；后续 scientific 声明是幂等重放，不是改写 authority。
    deviated = asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="environment_probe",
        reason="Probe a CUDA memory boundary for a frozen diagnostic preregistration.",
    ))
    classified = asyncio.run(run_contract._classify_experiment_scope(
        state, scope="scientific",
        reason="Run the bound secondary scientific CUDA diagnostic.",
    ))
    contract = run_contract.load_run_contract(state)

    assert deviated["status"] == "success", deviated
    assert deviated["classification"]["prereg_deviation"]["kind"] == \
        "requested_operation_with_governing_prereg"
    assert classified["status"] == "success"
    assert classified["classification"]["source"] == "governing_task_input_binding"
    assert classified["idempotent"] is True
    assert contract["execution_mode"] == "scientific"
    assert contract["run_role"] == "secondary"
    assert contract["analysis_eligible"] is False
    assert "execution_mode_missing_legacy_default" in contract["contract_warnings"]


def test_bound_prereg_explicit_operational_mode_is_overridden_by_governing_binding(tmp_path):
    state = _state(tmp_path)
    prereg_id = _save_frozen(
        state, "pre_registration", "operational_probe", "# prereg",
        metadata={
            "run_role": "secondary",
            "execution_mode": "operational", "operation_kind": "environment_probe",
        },
    )["id"]
    state.hook_state["node_inputs"] = {"prereg_artifact_id": prereg_id}

    _bind_operation_inputs(state)
    classified = asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="environment_probe",
        reason="Run the frozen operational environment probe without scientific analysis.",
    ))
    contract = run_contract.load_run_contract(state)

    assert classified["status"] == "success"
    assert classified["classification"]["requested_mode"] == "operational"
    assert classified["classification"]["mode"] == "scientific"
    assert contract["execution_mode"] == "scientific"
    assert contract["analysis_eligible"] is False
    assert state.hook_state["_request_mode"] == "scientific"


def test_operation_uses_one_frozen_evidence_triplet(tmp_path):
    state = _state(tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="package_install",
        reason="Install h5py and verify import/version without scientific simulation.",
    ))
    completion = _complete_operation(state)

    loop_result = SimpleNamespace(status="completed", final_text="done")
    hooks.experiment_contract_audit_on_end(SimpleNamespace(state=state), loop_result)
    events = state.transcript_path.read_text(encoding="utf-8")

    assert completion["status"] == "success", completion
    assert loop_result.status == "completed"
    assert "experiment_operation_audit" in events
    assert "operation evidence triplet is frozen" in events


def test_operation_audit_rejects_missing_or_duplicate_result_evidence(tmp_path):
    state = _state(tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="package_install",
        reason="Install a package and verify its import without scientific simulation.",
    ))
    completion = _complete_operation(state)
    state.save_artifact("raw_results", "duplicate_operation_raw", "{}", metadata={})

    audit = hooks._audit_operation_log(state)

    assert completion["status"] == "success", completion
    assert audit["passed"] is False
    assert any("exactly one current-run raw_results" in error for error in audit["result_evidence"]["errors"])


def test_operation_audit_ignores_a_prior_run_log(tmp_path):
    state = _state(tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="scheduler_probe",
        reason="Current run validates a scheduler submission independently.",
    ))
    completion = _complete_operation(state)
    current = completion["experiment_log_artifact_id"]
    historical_id = "experiment_log__prior_dry_run"
    historical = {
        "type": "experiment_log", "name": "prior_dry_run",
        "content": "status: dry_run", "metadata": {"frozen": True},
        "produced_by_run_id": "prior-run",
    }
    original_list, original_read = state.list_artifacts, state.read_artifact

    def list_artifacts(artifact_type=None, own_only=False):
        records = original_list(artifact_type, own_only=own_only)
        if artifact_type == "experiment_log":
            return [{"id": historical_id, "type": "experiment_log"}] + records
        return records

    def read_artifact(artifact_id):
        return historical if artifact_id == historical_id else original_read(artifact_id)

    state.list_artifacts, state.read_artifact = list_artifacts, read_artifact
    audit = hooks._audit_operation_log(state)
    assert audit["passed"] is True
    assert audit["artifact_id"] == current

def test_legacy_secondary_build_prereg_is_scientific_when_bound(tmp_path):
    state = _state(tmp_path)
    prereg_id = _save_frozen(
        state, "pre_registration", "legacy_build", "# legacy toolchain delivery",
        metadata={"run_role": "secondary", "stage": "toolchain_build"},
    )["id"]
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_spec": "Build with CMake, run CTest, package a tarball, and record its SHA256.",
    }

    _bind_operation_inputs(state)
    classified = asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="toolchain_build",
        reason="Build, CTest, tarball, and hash verification are a toolchain delivery only.",
    ))
    contract = run_contract.load_run_contract(state)

    assert classified["status"] == "success", classified
    assert classified["classification"]["requested_mode"] == "operational"
    assert classified["classification"]["mode"] == "scientific"
    assert contract["execution_mode"] == "scientific"
    assert contract["analysis_eligible"] is False


def test_legacy_secondary_mixed_signals_declare_operation_downgrade_deviation(tmp_path):
    state = _state(tmp_path)
    prereg_id = _save_frozen(
        state, "pre_registration", "legacy_mixed", "# legacy prereg",
        metadata={"run_role": "secondary", "stage": "toolchain_build"},
    )["id"]
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_spec": "Build the solver and compare the hypothesis threshold against simulation output.",
    }

    _bind_operation_inputs(state)
    # 判决拆除 O1（rc:683 降格，2026-08-31）：与绑定 prereg 冲突的 scope 声明改为
    # 申报偏离，不再被拒 —— 冲突这个事实照记，执行照走。
    deviated = asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="toolchain_build",
        reason="Build first, then compare the simulation result with the scientific threshold.",
    ))

    assert deviated["status"] == "success", deviated
    assert deviated["classification"]["prereg_deviation"]["kind"] == \
        "requested_operation_with_governing_prereg"


def test_verdict_obligation_ignores_caller_stage_726_first_cut(tmp_path):
    """#726 第一刀：primary scientific 的 verdict 义务不由 caller 的 stage 决定。

    重构前的活体病（见 #726 评论）：同一份冻结 prereg、同一个 primary/scientific
    身份，caller 只把嘴上的一个词从 simulation 换成 toolchain_build/diagnostic，
    就能让 requires_hypothesis_verdict 从 True 变 False —— 一个词逃掉科学裁决义务。
    去 stage 后：五种 stage 声明（含不声明）全部 True，义务只由身份决定。
    """
    for stage_word in (None, "simulation", "toolchain_build", "diagnostic", "build"):
        state = _state(tmp_path / f"s_{stage_word}")
        prereg_id = _save_frozen(
            state, "pre_registration", "H", "# prereg\n## Research Question\nx",
            metadata={"run_role": "primary",
                      "execution_mode": "scientific", "analysis_eligible": True},
        )["id"]
        node_inputs = {"prereg_artifact_id": prereg_id}
        if stage_word is not None:
            node_inputs["stage"] = stage_word
        state.hook_state["node_inputs"] = node_inputs
        asyncio.run(run_contract._classify_experiment_scope(
            state, scope="scientific", reason="primary scientific run"))
        contract = run_contract.load_run_contract(state)
        assert contract["requires_hypothesis_verdict"] is True, (
            f"stage={stage_word!r} 逃掉了 verdict 义务 —— stage 又回到判据里了")


def test_the_analysis_eligible_alias_is_a_derived_duplicate_pending_framework_cleanup(
    tmp_path,
):
    """`analysis_eligible` 只剩一个派生只读别名，值恒等于 requires_hypothesis_verdict。

    节点已不再解释它（AGENTS.md:143）。留着纯粹因为仓库根
    tests/test_run_contract_cross_node_read.py 有三处直接下标取这个键，而根测试归
    framework owner，单方面删键会把它们变成长红。
    owner=framework（wangd/jerry）；**删除条件**：那三处断言改掉之后，把
    load_run_contract、create_run_manifest 与 run_manifest artifact metadata 三处
    同名字段一起删，本用例随之删除。
    """
    from nodes.experiment.tools.run_contract import load_run_contract

    for role, expected in (("primary", True), ("secondary", False)):
        state = _state(tmp_path / role)
        prereg = _save_frozen(
            state, "pre_registration", "contract", "## frozen prereg\n",
            metadata={"run_role": role, "expected_params": {"n": 1}},
        )
        state.hook_state["node_inputs"] = {"prereg_artifact_id": prereg["id"]}
        contract = load_run_contract(state)
        assert contract["requires_hypothesis_verdict"] is expected, contract
        assert contract["analysis_eligible"] is contract[
            "requires_hypothesis_verdict"], contract


def test_a_witnessed_primary_run_still_gets_a_repro_bundle(tmp_path, monkeypatch):
    """bundle 判据改成"欠不欠正式裁决"：记过执行前提见证的 primary 照样出 bundle。

    原先判据是 analysis_eligible，见证会把它翻成 False，于是一个仍然欠裁决的运行
    反而不产出复现包——要交结论、却不留可复现材料。
    """
    state = _state(tmp_path)
    prereg = _save_frozen(
        state, "pre_registration", "contract", "## frozen prereg\n",
        metadata={"run_role": "primary", "expected_params": {"n": 1}},
    )
    state.hook_state["node_inputs"] = {"prereg_artifact_id": prereg["id"]}
    run_contract.record_execution_precondition_witness(
        state, "test", "formal input package unverified")
    monkeypatch.setattr(repro_snapshot, "_run", lambda *a, **k: "")
    calls: list[bool] = []
    real_create = repro_snapshot.create_repro_bundle

    def spy(state_, snap, result_info, **kwargs):
        calls.append(bool(kwargs.get("force")))
        return real_create(state_, snap, result_info, **kwargs)

    monkeypatch.setattr(repro_snapshot, "create_repro_bundle", spy)

    hooks._repro_snapshot_on_end(
        SimpleNamespace(state=state, turn=0),
        SimpleNamespace(status="completed", returncode=0))

    assert calls == [True], "欠裁决的运行必须出 bundle，见证不改变这件事"
    manifest_path = (state.root / "outputs" / "experiment" / "repro"
                     / "run_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["execution_precondition_witnesses"] == [
        {"source": "test", "reason": "formal input package unverified"}]
    assert manifest["requires_hypothesis_verdict"] is True
