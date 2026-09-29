from pathlib import Path

import re
import yaml


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture(name: str) -> dict:
    payload = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert isinstance(payload.get("node_inputs"), dict)
    return payload


def _focus(payload: dict) -> str:
    focus = payload["node_inputs"].get("experiment_focus")
    assert isinstance(focus, str) and focus.strip()
    return focus


def test_operation_fixture_records_triplet_before_external_finalize() -> None:
    focus = _focus(_fixture("e2e_local_scheduler_operation.yaml"))

    completion = focus.index("record_operation_completion(")
    finalize = focus.index("finalize_external_job", completion)
    assert completion < finalize
    assert "external_job_refs" in focus
    assert "container_runtime_id" in focus
    assert "不要 create_experiment" in focus
    assert "不要写 scientific verdict" in focus


def test_toolchain_fixture_is_a_non_scientific_operation() -> None:
    payload = _fixture("toolchain_sandbox_smoke.yaml")
    focus = _focus(payload)

    assert "upstream_artifacts" not in payload
    assert "scope=operation" in focus
    assert "operation_category=toolchain_build" in focus
    assert "stage=toolchain_build" in focus
    assert "build_and_run 的 submit_job route step" in focus
    assert "不要把 configure、build、run_smoke 拆成多个" in focus
    assert ".hf-route-ready" in focus
    assert "expected_outputs 只能声明 run_root 中的 smoke_stdout.txt" in focus
    assert "program=cmake" in focus
    assert "program_sequence" in focus
    assert '["cmake", "cmake", "<build_root>/hf_toolchain_smoke"]' in focus
    assert "submit_job 没有同名参数" in focus
    assert "静态内联 payload" in focus
    assert "route_step_id=build_and_run" in focus
    assert "output_paths 同时覆盖 <build_root> 与 <run_root>" in focus
    assert "不要使用任何外部 shell" in focus
    assert "run_smoke.sh" not in focus
    assert "不得 amend route 或重试该坏 receipt" in focus
    assert "record_operation_completion(" in focus
    assert 'task_kind="toolchain_build"' in focus
    assert "external_job_refs" in focus
    assert "不要写 Hypothesis Verdict" in focus
    assert "verdict:" not in focus
    assert "不要调用" in focus and "preview_experiment_contract" in focus


def test_secondary_scientific_fixture_uses_secondary_closure_only() -> None:
    payload = _fixture("e2e_local_scheduler_scientific.yaml")
    inputs = payload["node_inputs"]
    focus = _focus(payload)
    prereg = next(
        item for item in payload["upstream_artifacts"] if item.get("type") == "pre_registration"
    )

    # 真实调用不喂这两项：契约里 stage 是"兼容输入、可省略、非权威"（不得把
    # operation 变成 scientific），run_role "以冻结 prereg 的角色为准"。fixture
    # 刻意省略，逼节点走推导路径——也才测得到 stage 是否被违规当成授权来源。
    assert "run_role" not in inputs
    assert "stage" not in inputs
    assert inputs["prereg_artifact_id"] == ("pre_registration__Verlet_Energy_Drift_dt001")
    assert prereg["metadata"]["run_role"] == "secondary"
    assert prereg["metadata"]["analysis_eligible"] is False
    assert "verlet_simulation" in focus
    assert "action.program=python3" in focus
    assert "route_step_id=verlet_simulation" in focus
    assert 'expected_outputs=["verlet_result.json"]' in focus
    assert "不得调用 primary-only" in focus
    assert "resolve_prereg_questions → freeze_artifact" not in focus
    assert "（可选）create_experiment" not in focus
    assert focus.index("create_experiment") < focus.index("finalize_external_job")


def test_primary_fixture_binds_params_and_full_primary_closure() -> None:
    payload = _fixture("e2e_local_scheduler_primary.yaml")
    inputs = payload["node_inputs"]
    focus = _focus(payload)
    prereg = next(
        item for item in payload["upstream_artifacts"] if item.get("type") == "pre_registration"
    )
    expected = prereg["metadata"]["execution_contract"]["scientific_params"]

    # 真实调用不喂这两项：契约里 stage 是"兼容输入、可省略、非权威"（不得把
    # operation 变成 scientific），run_role "以冻结 prereg 的角色为准"。fixture
    # 刻意省略，逼节点走推导路径——也才测得到 stage 是否被违规当成授权来源。
    assert "run_role" not in inputs
    assert "stage" not in inputs
    assert inputs["execution_params"] == expected
    assert "verlet_primary_simulation" in focus
    assert "action.program=python3" in focus
    assert "route_step_id=verlet_primary_simulation" in focus
    assert 'expected_outputs=["verlet_result.json"]' in focus
    assert "不能包装成 shell script" in focus
    assert "external_job_workflow" in focus
    assert "submission_nonce" in focus
    assert "container_runtime_id" in focus
    assert "不能从 sandbox_state" in focus
    assert "（可选）create_experiment" not in focus
    assert focus.index("resolve_prereg_questions") < focus.index("assess_sediment_candidate")
    assert focus.index("assess_sediment_candidate") < focus.index("freeze_artifact")
    assert focus.index("freeze_artifact") < focus.index("create_experiment")
    assert focus.index("create_experiment") < focus.index("finalize_external_job")


def test_fresh_lammps_fixture_separates_toolchain_from_scientific_execution() -> None:
    payload = _fixture("fresh/04_lammps_nve_energy_drift.yaml")
    focus = _focus(payload)
    prereg = next(
        item for item in payload["upstream_artifacts"] if item.get("type") == "pre_registration"
    )
    contract = prereg["metadata"]["execution_contract"]
    params = contract["scientific_params"]
    prereg_content = prereg["content"]
    flat_focus = " ".join(focus.split())
    flat_prereg = " ".join(prereg_content.split())

    assert prereg["metadata"]["frozen"] is True
    assert prereg["metadata"]["run_role"] == "primary"
    assert prereg["metadata"]["analysis_eligible"] is True
    assert params["timesteps"] == [0.002, 0.012]
    assert params["steps"] == 5000
    assert params["max_relative_drift"] == 0.02

    assert "H2-C scientific-only" in flat_focus
    assert "frozen H2-B operation raw_results and experiment_log" in flat_focus
    assert "same Project worktree" in flat_focus
    assert "explicitly forwarded frozen input" in flat_focus
    assert "outcome=success" in flat_focus
    assert "record_kind=operation" in flat_focus
    assert "source/final URL" in flat_focus
    assert "archive and executable SHA-256" in flat_focus
    assert "shared_filesystem storage binding" in flat_focus
    assert "recheck the locator exists" in flat_focus
    assert "environment_lock_artifact_ids" in flat_focus
    assert "deployment-provided immutable target_profile_ref" in flat_focus
    assert "discover_resources is observation only" in flat_focus
    assert "must not reuse the H2-B route or envelope" in flat_focus
    assert "structured blocker" in flat_focus
    assert "do not submit a scientific job" in flat_focus
    assert "fabricate trajectories" in flat_focus

    assert "fetch_resource" not in flat_focus
    assert "controlled extraction" not in flat_focus
    assert "controlled local native-solver supply path" not in flat_focus
    assert "patch_4Jul2026" not in flat_focus
    assert "stable_22Jul2025_update5.tar.gz" not in flat_focus

    assert "H2-B boundary" in flat_prereg
    assert "toolchain_build" in flat_prereg
    assert "raw_results and experiment_log" in flat_prereg
    assert "This operation evidence cannot substitute" in flat_prereg
    assert "H2-C revalidation" in flat_prereg
    assert "new evidence_bearing execution envelope" in flat_prereg
    assert "do not acquire, extract, build" in flat_prereg
    assert "complete commit" not in flat_prereg
    assert "patch_4Jul2026" not in flat_prereg


# ── 真实调用形态的 fixture：机械守住"不许把答案抄给节点" ────────────────────
#
# e2e_realistic_scientific.yaml 的价值全在"什么都不说"：真实 orchestrator→experiment
# 往往只给一句目标（+ 科学 run 的 prereg id），节点必须自己判 scope、自己知道要先绑
# envelope 与冻 route、自己推 secondary 闭包顺序、自己避开 primary-only 工具。
# 一旦有人"好心"把工具名/参数/步骤加回去，它就退化成脚本化回归（那是
# e2e_local_scheduler_scientific.yaml 的职责），本 fixture 也就失去存在意义。
# 这条测试机械地守住这条线。

_SCRIPTED_TOOL_NAMES = (
    "classify_experiment_scope", "declare_execution_envelope",
    "declare_execution_route", "submit_job", "wait_for_external_job",
    "freeze_artifact", "save_artifact", "preview_experiment_contract",
    "create_experiment", "finalize_external_job",
    "resolve_prereg_questions", "resolve_prereg_hypotheses",
    "assess_sediment_candidate",
)
_SCRIPTED_ARG_MARKERS = (
    "assurance_class", "route_step_id", "expected_outputs", "action.tool",
    "action.program", "storage_binding", "environment_lock_artifact_ids",
    "evidence_refs", "dry_run", "workdir_role",
)


def test_realistic_fixture_gives_a_goal_not_a_tool_script() -> None:
    payload = _fixture("e2e_realistic_scientific.yaml")
    inputs = payload["node_inputs"]
    focus = _focus(payload)

    # 只给"目标 + prereg 绑定"，不喂任何可推导/非权威项。
    assert set(inputs) == {"prereg_artifact_id", "experiment_focus"}, sorted(inputs)

    # 不含工具名、参数名、步骤编号——否则就是把答案抄给节点。
    for name in _SCRIPTED_TOOL_NAMES:
        assert name not in focus, f"realistic fixture 不得点名工具：{name}"
    for marker in _SCRIPTED_ARG_MARKERS:
        assert marker not in focus, f"realistic fixture 不得规定参数：{marker}"
    assert not re.search(r"(?m)^\s*\d+\.\s", focus), "realistic fixture 不得写步骤编号"

    # 指令应当简短（真实调用是一句话，不是一页脚本）。
    assert len(focus.strip().splitlines()) <= 10

    # 推导源必须完好：run_role / analysis_eligible 的权威在冻结 prereg，不在调用方。
    prereg = next(
        item for item in payload["upstream_artifacts"] if item.get("type") == "pre_registration"
    )
    assert prereg["metadata"]["frozen"] is True
    assert prereg["metadata"]["run_role"] == "secondary"
    assert prereg["metadata"]["analysis_eligible"] is False


def test_realistic_fixture_delivers_the_source_instead_of_pointing_at_the_repo():
    """源码必须随 artifact 交付，不能靠"去仓库里找"。

    2026-09-02 真实 E2E：prereg 写着"积分器在本 harness-framework 仓库
    nodes/experiment/fixtures/verlet_e2e_src/verlet.py"。但 <project>/workspace
    是空的项目级暂存区（core/state.py:46 mkdir），不是仓库检出——节点从它站的
    位置解析不了这个引用。它烧了 ~18 轮翻文件系统，最后撞上一份陈旧副本，
    据此提交作业（沙箱看不见宿主机路径，exit_code=2），再改用 safe_write_file
    把源码誊写进 run_root 才跑通——源码溯源链就此断掉。

    走 stage_in 也不行：它要求 src 落在本 run 的可读根内（resource_manager.py
    _stage_in_readable），仓库路径除非被授予 path role 否则不在其中；而 path role
    只接受绝对路径（path_roles.py _normalize_path 要求 / 或 ~ 开头，这是对的：
    权限不能按 cwd 解析），fixture 里写死绝对路径就又回到 616952df 修掉的
    "作者机器布局"老问题。

    因此对可移植 fixture 而言，artifact 交付是唯一既真实又可解析的通道。
    """
    fixture = _fixture("e2e_realistic_scientific.yaml")
    bundles = [a for a in fixture["upstream_artifacts"] if a["type"] == "source_bundle"]
    assert len(bundles) == 1, "realistic fixture 必须随上游交付积分器源码"

    on_disk = (FIXTURES / "verlet_e2e_src" / "verlet.py").read_text(encoding="utf-8")
    assert bundles[0]["content"] == on_disk, "内嵌源码与 verlet_e2e_src/verlet.py 漂移了"

    # 任何"去仓库里找源码"的指路都会把节点带回那 18 轮
    prereg = next(a for a in fixture["upstream_artifacts"] if a["type"] == "pre_registration")
    for text in (fixture["node_inputs"]["experiment_focus"], prereg["content"]):
        assert "verlet_e2e_src" not in text, text
        assert "harness-framework 仓库" not in text, text
