"""预期终止（第 5 步 5b；design_expected_termination_0914.md v2）。

任务本身要求作业以非零码退出（验证失败能否被正常收尾）时，原先没有合法的收尾路：记失败则
后续步骤被锁、ROC 拒 success。改为：
- submit_job 顶层 expected_termination{exit_codes 1..123, task_quote}，task_quote 必须逐字
  出自本 run 首条 loop_seed 的 user 消息、每个码以独立数字出现，否则确认卡之前拒绝；
- 收尾结果仍跟着物理事实走（exit≠0 记 operation_failed），另机械判定 termination_matched；
- 路线步骤与 ROC 读 termination_matched。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import pytest

from core import sandbox
from core.state import State
from nodes.experiment.tools import execution_route
from nodes.experiment.tools import operation_completion as oc
from nodes.experiment.task_prose_inputs import (
    TASK_PROSE_INPUT_KEYS,
    freeze_initial_task_prose_source,
)
from nodes.experiment.tools import resource_manager as manager
from test_external_cancel_transaction import _cleanup, requires_sandbox
from test_external_route_projection import _events, _submitted_route
from test_operation_job_finalization import (
    _external_job_ref, _finalize, _health, _patch_pipeline, _prepared_state, _submission,
)
from test_resource_manager import _planned_state, trusted_sandbox  # noqa: F401

_FINAL_LIVE_TASK = (
    "提交两个作业：\n    - 作业 A：正常结束，退出码 0（例如 command 为 'echo JOB_A_OK; exit 0'）\n"
    "    - 作业 B：以退出码 3 结束（例如 command 为 'echo JOB_B_FAIL; exit 3'）\n"
    "2. 对每个作业，分别取它自己的标准输出（stdout）和退出码作为证据。")
_FINAL_LIVE_QUOTE = "作业 B：以退出码 3 结束（例如 command 为 'echo JOB_B_FAIL; exit 3'）"
_ACC1_TASK = ("命令为 python3 -c \"import sys;print('starting',flush=True);sys.exit(3)\"，"
              "内存 1 GiB，walltime 5 分钟，预期时长 20 秒。该作业会以退出码 3 失败（预期内），"
              "目的是验证失败后能否被正常收尾。")
_ACC1_QUOTE = "该作业会以退出码 3 失败（预期内）"
_PARTIAL_VALIDATION_TASK = (
    "请按 README 原样对全部读数执行一次处理，不要修改 `input.csv` 或处理程序。全部读数都处理"
    "成功，这批读数才算接收成功；没能全部处理成功时请如实说明。")
_DECLARED = {"exit_codes": [3], "task_quote": _FINAL_LIVE_QUOTE,
             "anchor": "experiment_spec"}

#: 三份活体首条 loop_seed 的 user 消息原文（final-live 1789236325-9c5499、acc1
#: 1789030271-a02978、acc2 1789088689-050d11）。含框架的 KB 状态样板与产物提醒——
#: 37a2736b 的锚点测试用干净摘录，没测出样板里的数字能锚住退出码（第三会话复审 P1）。
_REAL_SEED_USER_MESSAGES = {
    "final_live": "## 节点输入\n\n- **experiment_spec**：运维核验（operation 类，非科学实验，不需要 prereg/科学裁决）：核验受管本地作业（submit_job scheduler=local）的退出码是否如实传播到作业记录。\n\n要求：\n1. 用 submit_job(scheduler='local') 提交两个作业：\n   - 作业 A：正常结束，退出码 0（例如 command 为 'echo JOB_A_OK; exit 0'）\n   - 作业 B：以退出码 3 结束（例如 command 为 'echo JOB_B_FAIL; exit 3'）\n2. 对每个作业，分别取它自己的标准输出（stdout）和退出码作为证据。\n3. 判断：作业实际退出码（0 和 3）有没有如实写到作业记录上（job_submission / health 里的 exit_code 字段），即退出码是否如实传播。\n4. 两个作业最后都要正常收尾（都走到 exited 状态、记录里有 exit_code），不要留下悬空/卡住的作业。\n\n交付：一份 experiment_log 记录两个作业各自的 stdout、实际退出码、作业记录里记录的 exit_code，以及'退出码是否如实传播'的判断结论。\n\n## 提醒：必须产出的 artifact 类型 = ['raw_results', 'clean_results', 'experiment_log']。在结束前用这些 `artifact_type` 调用 `save_artifact`。",
    "acc1": "## 节点输入\n\n- **experiment_spec**：在 experiment 的 run 目录下提交一个受管本地作业，命令为 python3 -c \"import sys;print('starting',flush=True);sys.exit(3)\"，内存 1 GiB，walltime 5 分钟，预期时长 20 秒。该作业会以退出码 3 失败（预期内），目的是验证失败后能否被正常收尾。跑完并按机械证据收尾后，把命令改成 sys.exit(0) 重新提交一次并收尾。两次的作业标识、退出码、收尾结果都要记录。全程离线，不修改附件内容。\n\n## 项目 KB 状态（💡 历史记录参考，**不是 ground truth**）\n\nKB 内已有（已过滤 invalid status）：0 concept / 0 claim ((no breakdown)) / 0 experiment / 1 chunk。\n\n**使用原则**（用 KB 但不被 KB 锁死）：\n- 写新 claim/concept 前先 `search_kb` 找已有 —— 找到的 id 必须复用\n- 看每条 claim 的 **scope_dimensions** —— 你当前任务跟它同 scope 吗？\n  - scope 不同 → 你的判断**优先**于 KB，不必受约束（dead_end 也一样：仅在其 scope 内成立）\n  - scope 同但你认为 claim 过时 / 错 → 在你的 artifact 里**显式写**：\n    `I disagree with claim_<id> because <reason>` —— curator 会扫到并 propose review\n  - **不要**默认 trust KB；**也不要**静默 ignore\n- 看到 ⚠️ disputed 标的 claim → 这是 active controversy，不是定论\n- 看到 superseded 标的 claim → 跳到 `superseded_by` 指的新版\n\n（用 query='hypothesis, falsification_criteria, claim_status' 没命中，但 KB 里有上面提到的那些条目。你可以用别的 query 再查。）\n\n## 提醒：必须产出的 artifact 类型 = ['raw_results', 'clean_results', 'experiment_log']。在结束前用这些 `artifact_type` 调用 `save_artifact`。",
    "acc2": "## 节点输入\n\n- **experiment_spec**：科学探索性运行（无预注册，exploratory，不裁决已知规则）。目标：用实测数据判断这台机器上 Python 进程退出码传播是否可靠。\n\n任务：在 experiment 的 run 目录下提交两个受管本地作业并各自跑完收尾：\n1) 作业 A：命令 `python3 -c \"import sys;print('starting',flush=True);sys.exit(3)\"`，内存 1 GiB，walltime 5 分钟，预期时长 20 秒。预期以退出码 3 结束（失败），要验证它失败后能否被正常收尾。\n2) 作业 B：把命令改成 `python3 -c \"import sys;print('starting',flush=True);sys.exit(0)\"`，同样内存 1 GiB、walltime 5 分钟、预期时长 20 秒，重新提交并收尾。\n\n交付：两次作业各自的作业标识（job id）、实际退出码、收尾结果（是否被正常收尾、状态判定），以及一份可机读汇总（machine-readable summary）。全程离线，不修改任何附件内容。任何阶段未真正运行、进程未相互独立、或状态不一致，都应明确记为未通过，不得用预先计算的答案补齐。\n\n## 项目 KB 状态（💡 历史记录参考，**不是 ground truth**）\n\nKB 内已有（已过滤 invalid status）：0 concept / 0 claim ((no breakdown)) / 0 experiment / 2 chunk。\n\n**使用原则**（用 KB 但不被 KB 锁死）：\n- 写新 claim/concept 前先 `search_kb` 找已有 —— 找到的 id 必须复用\n- 看每条 claim 的 **scope_dimensions** —— 你当前任务跟它同 scope 吗？\n  - scope 不同 → 你的判断**优先**于 KB，不必受约束（dead_end 也一样：仅在其 scope 内成立）\n  - scope 同但你认为 claim 过时 / 错 → 在你的 artifact 里**显式写**：\n    `I disagree with claim_<id> because <reason>` —— curator 会扫到并 propose review\n  - **不要**默认 trust KB；**也不要**静默 ignore\n- 看到 ⚠️ disputed 标的 claim → 这是 active controversy，不是定论\n- 看到 superseded 标的 claim → 跳到 `superseded_by` 指的新版\n\n（用 query='hypothesis, falsification_criteria, claim_status' 没命中，但 KB 里有上面提到的那些条目。你可以用别的 query 再查。）\n\n## 提醒：必须产出的 artifact 类型 = ['raw_results', 'clean_results', 'experiment_log']。在结束前用这些 `artifact_type` 调用 `save_artifact`。",
}


def _rendered_user_message(spec: str) -> str:
    """按 core/context_engine._build_user_prompt 的渲染形状造 user 消息。"""
    return (f"## 节点输入\n\n- **experiment_spec**：{spec}\n\n"
            "## 提醒：必须产出的 artifact 类型 = ['raw_results', 'clean_results', "
            "'experiment_log']。")


def _seed(state: State, user_text: str, system_text: str = "规则正文", *,
          rendered: bool = False) -> None:
    content = user_text if rendered else _rendered_user_message(user_text)
    user_channels = ["node_inputs"]
    if "## 可用的上游 artifact（用 `read_artifact` 查看）" in content:
        user_channels.append("upstream_artifacts")
    if "## 项目 KB 状态（💡 历史记录参考，**不是 ground truth**）" in content:
        user_channels.append("kb_context")
    if "## 提醒：必须产出的 artifact 类型 =" in content:
        user_channels.append("required_outputs_reminder")
    state.append_transcript(
        "startup_injection_manifest",
        node_input_keys=["experiment_spec"],
        user=user_channels,
    )
    state.append_transcript("loop_seed", n_messages=2, messages=[
        {"role": "system", "content": system_text},
        {"role": "user", "content": content},
    ])


# ── 锚点 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("task, quote", [
    (_FINAL_LIVE_TASK, _FINAL_LIVE_QUOTE),
    (_ACC1_TASK, _ACC1_QUOTE),
], ids=["final_live", "acc1"])
def test_a_quote_copied_from_the_task_anchors_the_declaration(tmp_path, task, quote):
    state = State.new("experiment", tmp_path)
    _seed(state, task)

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert refusal is None, refusal
    assert normalized == {"exit_codes": [3], "task_quote": quote,
                          "anchor": "experiment_spec"}


@pytest.mark.parametrize("seed, quote", [
    ("final_live", "作业 B：以退出码 3 结束（例如 command 为 'echo JOB_B_FAIL; exit 3'）"),
    ("acc1", "该作业会以退出码 3 失败（预期内）"),
    ("acc2", "预期以退出码 3 结束（失败）"),
])
def test_the_real_task_texts_that_state_an_exit_code_still_anchor(tmp_path, seed, quote):
    state = State.new("experiment", tmp_path)
    _seed(state, _REAL_SEED_USER_MESSAGES[seed], rendered=True)

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert refusal is None, refusal
    assert normalized["exit_codes"] == [3]


@pytest.mark.parametrize("seed, codes, quote", [
    ("acc1", [1], "0 experiment / 1 chunk"),        # 框架 KB 状态样板
    ("acc1", [1], "内存 1 GiB"),                     # 资源量
    ("acc2", [2], "2 chunk"),                        # 框架 KB 状态样板
    ("acc2", [2], "2) 作业 B"),                      # 编号
    ("final_live", [2], "2. 对每个作业，分别取它自己的标准输出（stdout）和退出码作为证据。"),
    ("final_live", [1], "1. 用 submit_job(scheduler='local') 提交两个作业："),
], ids=["kb_boilerplate_1", "memory_1_gib", "kb_boilerplate_2", "item_2_paren",
        "item_2_dot", "item_1_dot"])
def test_a_number_in_the_real_user_message_that_is_not_an_exit_code_does_not_anchor(
    tmp_path, seed, codes, quote,
):
    """第三会话复审 P1 的六格（探针 review-0914-third/probe_expected_termination）：引文逐字
    在真实 user 消息里、码是独立数字，但这个数字不是退出码。修复前全部放行，普通失败
    随即被判为「符合预期」。"""
    state = State.new("experiment", tmp_path)
    _seed(state, _REAL_SEED_USER_MESSAGES[seed], rendered=True)
    assert quote in _REAL_SEED_USER_MESSAGES[seed]

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": codes, "task_quote": quote})

    assert normalized is None, normalized
    assert refusal["error_code"] == "expected_termination_not_anchored"
    assert refusal["next_actions"][0].startswith("先修正调用输入")


@pytest.mark.parametrize("user_text, system_text, declaration, problem", [
    (_PARTIAL_VALIDATION_TASK, "规则正文",
     {"exit_codes": [3], "task_quote": "没能全部处理成功时请如实说明"}, "紧跟"),
    ("任务里什么都没说", _FINAL_LIVE_QUOTE,
     {"exit_codes": [3], "task_quote": _FINAL_LIVE_QUOTE}, "逐字出自"),
    ("该作业会以退出码 13 失败（预期内）", "规则正文",
     {"exit_codes": [3], "task_quote": "该作业会以退出码 13 失败（预期内）"}, "紧跟"),
    (_FINAL_LIVE_TASK, "规则正文", {"exit_codes": [0], "task_quote": _FINAL_LIVE_QUOTE}, "1..123"),
    ("超时返回码 124 属于预期", "规则正文",
     {"exit_codes": [124], "task_quote": "超时返回码 124 属于预期"}, "1..123"),
    ("被信号杀掉 -9 属于预期", "规则正文",
     {"exit_codes": [-9], "task_quote": "被信号杀掉 -9 属于预期"}, "1..123"),
], ids=["partial_validation_failure", "only_in_system_prompt", "code_inside_13",
        "zero", "timeout_124", "negative"])
def test_a_declaration_the_task_does_not_back_is_refused(
    tmp_path, user_text, system_text, declaration, problem,
):
    state = State.new("experiment", tmp_path)
    _seed(state, user_text, system_text)

    normalized, refusal = manager._anchored_expected_termination(state, declaration)

    assert normalized is None
    assert refusal["error_code"] == "expected_termination_not_anchored", refusal
    assert any(problem in item for item in refusal["problems"]), refusal
    assert refusal["side_effects"] == "none"


@pytest.mark.parametrize("dry_run", [False, True])
def test_submit_job_refuses_an_unanchored_declaration_before_the_card(
    tmp_path, trusted_sandbox, dry_run,  # noqa: F811
):
    state = _planned_state(tmp_path)
    _seed(state, _PARTIAL_VALIDATION_TASK)

    result = asyncio.run(manager._submit_job(
        state=state, command="python3 process_readings.py", scheduler="local",
        dry_run=dry_run, execution_params={"case": "test"},
        expected_termination={"exit_codes": [3], "task_quote": "没能全部处理成功时请如实说明"},
    ))

    assert result["status"] == "error", result
    assert result["error_code"] == "expected_termination_not_anchored"
    assert state.list_artifacts("job_submission") == []
    assert "_highrisk_pending" not in json.dumps(list(state.hook_state))


def test_an_anchored_declaration_is_shown_on_required_confirmation_card(
    tmp_path, trusted_sandbox,  # noqa: F811
):
    state = _planned_state(tmp_path)
    _seed(state, _FINAL_LIVE_TASK)

    result = asyncio.run(manager._submit_job(
        state=state, command="sudo sh -c 'echo JOB_B_FAIL; exit 3'", scheduler="local",
        dry_run=False, execution_params={"case": "test"},
        expected_termination={"exit_codes": [3], "task_quote": _FINAL_LIVE_QUOTE},
    ))

    assert result["status"] == "pause", result
    assert "expected_termination" in json.dumps(result, ensure_ascii=False)
    assert _FINAL_LIVE_QUOTE in json.dumps(result, ensure_ascii=False)


# ── finalize：结果词跟着物理事实，判定另记 ─────────────────────────────────────


def _receipt_health(state: State) -> dict:
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1, closures
    return json.loads(state.read_artifact(closures[0]["id"])["content"])["health"]


@pytest.mark.parametrize("exit_code, errors, matched", [
    (3, [], True),
    (0, [], False),
    (3, [{"path": "/logs/err.log", "marker": "HARNESS_SANDBOX_LIMIT"}], False),
], ids=["exit_3_as_declared", "exit_0_not_as_declared", "platform_limit_marker"])
def test_a_declared_job_is_finalized_as_it_physically_ended(
    tmp_path, monkeypatch, exit_code, errors, matched,
):
    submission = _submission(expected_termination=dict(_DECLARED))
    state = _prepared_state(tmp_path, submission)
    route_calls: list[dict] = []
    _patch_pipeline(monkeypatch, _health(
        error_evidence=errors,
        scheduler_result={"raw": {"sandbox_state": {"exit_code": exit_code}}}), route_calls)

    completed = _finalize(state, submission, "operation_completed")
    assert completed["status"] == "error", completed
    if not errors:  # 有错误证据时拒绝文案先说错误证据
        assert "预期终止" in completed["error"]

    failed = _finalize(state, submission, "operation_failed")

    assert failed["status"] == "success", failed
    health = _receipt_health(state)
    assert health["exit_code"] == exit_code
    assert health["termination_matched"] is matched
    assert health["expected_termination"]["exit_codes"] == [3]
    assert route_calls[-1]["domain_outcome"] == "operation_failed"
    assert route_calls[-1]["termination_matched"] is matched


def test_an_undeclared_job_keeps_the_old_receipt_shape(tmp_path, monkeypatch):
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    route_calls: list[dict] = []
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 3}}}), route_calls)

    assert _finalize(state, submission, "operation_failed")["status"] == "success"

    health = _receipt_health(state)
    assert "termination_matched" not in health and "expected_termination" not in health
    assert route_calls[-1]["termination_matched"] is False


# ── ROC / 核验：读判定 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("declared, successful", [(True, True), (False, False)])
def test_operation_verification_reads_the_frozen_termination_verdict(
    tmp_path, monkeypatch, declared, successful,
):
    submission = _submission(**({"expected_termination": dict(_DECLARED)} if declared else {}))
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 3}}}))
    assert _finalize(state, submission, "operation_failed")["status"] == "success"

    verification = oc._managed_external_job_verification(
        state, None, [_external_job_ref(submission)])

    assert verification["ok"] is True, verification
    assert verification["successful"] is successful, verification
    evidence = verification["health"][0]["success_evidence"]
    assert evidence["succeeded"] is False  # 物理事实：退出码 3 不是成功
    assert evidence.get("termination_matched", False) is successful


def _local_record(**overrides) -> dict:
    return {"scheduler": "local", "job_id": "hf-job-expected-exit",
            "container_runtime_id": "c" * 64, **overrides}


def _local_health(exit_code: int, *, oom: bool = False) -> dict:
    return _health(scheduler_result={"status": "success", "raw": {"ok": True, "sandbox_state": {
        "exists": True, "id": "c" * 64, "name": "hf-job-expected-exit", "managed": True,
        "kind": "job", "running": False, "status": "exited", "exit_code": exit_code,
        "oom_killed": oom}}})


@pytest.mark.parametrize("record, health, matched", [
    (_local_record(expected_termination=dict(_DECLARED)), _local_health(3), True),
    (_local_record(expected_termination=dict(_DECLARED)), _local_health(3, oom=True), False),
    (_local_record(expected_termination=dict(_DECLARED)), _local_health(0), False),
    (_submission(scheduler="pbs", expected_termination=dict(_DECLARED)),
     _health(scheduler_result={"raw": {"ok": True, "stdout": "job_state = F\nexit_status = 3\n"}}),
     True),
], ids=["local_exit_3", "local_oom_observed", "local_exit_0", "pbs_exit_status_3"])
def test_scheduler_evidence_carries_the_termination_verdict(record, health, matched):
    evidence = oc._external_job_success_evidence(record, health, {"status": None})

    assert evidence["verified"] is True, evidence
    assert evidence["termination_matched"] is matched, evidence


def test_a_declared_remote_job_cannot_fall_back_to_completion_paths():
    record = _submission(expected_termination=dict(_DECLARED))  # slurm，读不到退出码
    submitted = datetime.fromisoformat(record["submitted_at"]).timestamp()
    health = _health(completion_paths=[
        {"path": "/tmp/op/out.done", "exists": True, "mtime_epoch_s": submitted + 120.0}])

    evidence = oc._external_job_success_evidence(record, health, {"status": None})

    assert evidence["verified"] is False, evidence


# ── 路线：operation_failed + 判定符合 → 步骤 verified ──────────────────────────


@pytest.mark.parametrize("matched, route_state", [(True, "complete"), (False, "blocked")])
def test_a_failed_job_that_ended_as_declared_completes_its_route_step(
    tmp_path, matched, route_state,
):
    state = State.new("experiment", tmp_path / "runs", project_id="expected-termination")
    state.hook_state["_request_mode"] = "operation"
    state.hook_state["experiment_execution_scope"] = {"mode": "operational", "category": "other"}
    _binding, reference = _submitted_route(state)

    projected = execution_route.record_external_route_finalization(
        state,
        scheduler=reference["scheduler"], job_id=reference["job_id"],
        namespace=reference["namespace"], launch_host=reference["launch_host"],
        scheduler_cluster=reference["scheduler_cluster"],
        resource_uid=reference["resource_uid"],
        submission_nonce=reference["submission_nonce"],
        process_group_id=reference["process_group_id"],
        process_start_ticks=reference["process_start_ticks"],
        domain_outcome="operation_failed",
        evidence_artifact_id="external_job_operation_closure__receipt",
        termination_matched=matched,
    )

    assert projected["status"] == "success", projected
    assert execution_route.build_route_snapshot(state)["route_state"] == route_state
    verified = _events(state, "route_step_external_execution_verified")
    assert bool(verified) is matched
    if matched:
        evidence = verified[0]["verification_receipt"]["success_evidence"]
        assert evidence["succeeded"] is False and evidence["termination_matched"] is True


# ── 真实受管本地作业（L237）──────────────────────────────────────────────────


@requires_sandbox
def test_a_real_job_that_exits_3_as_the_task_said_passes_operation_verification(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    state = State.new("experiment", tmp_path / "run")
    state.hook_state["_request_mode"] = "operation"
    state.hook_state["experiment_execution_scope"] = {"mode": "operational", "category": "other"}
    submission: dict = {}
    try:
        runtime = manager.experiment_output_dir(state, "runtime", create=True).resolve()
        submission = manager._submit_sync(
            runtime_root=runtime, scheduler="local", command="echo JOB_B_FAIL; exit 3",
            job_name="expected-exit-3", mpi_ranks=1, cpus_per_rank=1, gpus=0,
            memory_gb=1.0, storage_gb=1.0, walltime_minutes=1, queue=None,
            nodelist=None, image=None, workdir=str(runtime), dry_run=False,
            namespace=None, output_paths=[str(runtime)], stage_in=None, state=state,
            submission_nonce="expected-exit-3", route_attempt_id="expected-exit-3",
            hard_deadline_s=45,
        )
        assert submission["status"] == "success", submission
        # _submit_job 在锚点核对通过后，把规范化声明写进同一份提交记录。
        submission["expected_termination"] = dict(_DECLARED)
        state.save_artifact("job_submission", "expected_exit_3", json.dumps(submission))
        deadline = time.monotonic() + 15
        while sandbox.inspect_container(submission["container_runtime_id"]).get("status") != "exited":
            assert time.monotonic() < deadline, "job did not exit"
            time.sleep(0.05)

        finalized = asyncio.run(manager._finalize_external_job(
            state, "local", submission["job_id"], "", "operation_failed",
            note="task says job B exits 3"))
        assert finalized["status"] in {"success", "finalized_needs_route_reconciliation"}, finalized
        health = _receipt_health(state)
        assert health["exit_code"] == 3 and health["termination_matched"] is True

        verification = oc._managed_external_job_verification(
            state, None, [_external_job_ref(submission)])
        assert verification["successful"] is True, verification
    finally:
        _cleanup(submission)


# ── 第三会话复审 0914c P3：裸 exit / return 与渲染格式 ──────────────────────────


@pytest.mark.parametrize("quote, codes, anchored", [
    ("the script should return 2 files to the caller", [2], False),
    ("exit 1 of 3 doors leads outside", [1], False),
    ("the helper exited with code 3 on purpose", [3], True),
    ("该作业预期状态码 3", [3], True),
    ("命令为 'echo JOB_B_FAIL; exit 3'", [3], True),
    ("python3 -c 'import sys; sys.exit(3)'", [3], True),
], ids=["return_2_files", "exit_1_of_3_doors", "exited_with_code", "status_code_zh",
        "bare_exit_quoted", "sys_exit_call"])
def test_bare_exit_and_return_count_only_before_punctuation_or_line_end(
    tmp_path, quote, codes, anchored,
):
    state = State.new("experiment", tmp_path)
    _seed(state, f"任务：{quote}")

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": codes, "task_quote": quote})

    assert (refusal is None) is anchored, refusal


def test_the_anchor_reads_the_spec_as_core_actually_renders_it(tmp_path):
    """直接用 core 的 _build_user_prompt 渲染 experiment_spec：core 改了节点输入的渲染格式，
    这条转红，而不是让锚点静默一律拒绝。"""
    state = State.new("experiment", tmp_path)
    spec = "作业 B：以退出码 3 结束（例如 command 为 `echo JOB_B_FAIL; exit 3`）"
    rendered = _seed_with_core_inputs(state, {"experiment_spec": spec})

    assert manager._task_text_for_anchor(state).strip() == spec
    _normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": spec})
    assert refusal is None, refusal


# C3a: declared task-prose inputs through the live Core renderer.
_NODE_INPUT_HEADING = "\u8282\u70b9\u8f93\u5165"
_FULLWIDTH_COLON = "\uff1a"


def _seed_with_core_inputs(state: State, inputs: dict[str, object]) -> str:
    """Use the live Core startup shape, then freeze the turn-one receipt."""
    from core.context_engine import build_messages
    from core.loader import load_harness

    state.hook_state["node_inputs"] = dict(inputs)
    messages = build_messages(load_harness("experiment"), state, inputs)
    rendered = next(
        str(message.content) for message in messages if message.role == "user"
    )
    state.append_transcript(
        "loop_seed", n_messages=1,
        messages=[{"role": "user", "content": rendered}],
    )
    freeze_initial_task_prose_source(state)
    return rendered



def test_turn_one_hook_freezes_the_real_core_input_source(tmp_path):
    from core.context_engine import build_messages
    from core.loader import load_harness
    from core.loop_hooks import HookContext
    from nodes.experiment import hooks

    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    inputs = {"experiment_focus": quote}
    state.hook_state["node_inputs"] = dict(inputs)
    harness = load_harness("experiment")
    messages = build_messages(harness, state, inputs)
    state.append_transcript(
        "loop_seed", n_messages=len(messages),
        messages=[{"role": message.role, "content": message.content} for message in messages],
    )

    hooks._turn_one_briefing_on_turn_start(
        HookContext(harness=harness, state=state, messages=messages, turn=1),
    )
    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert refusal is None, refusal
    assert normalized["anchor"] == "experiment_focus"
    assert state.transcript_path.read_text(encoding="utf-8").count(
        "experiment_task_prose_input_receipt"
    ) == 1


def test_receipt_rejects_a_same_key_value_changed_after_loop_seed(tmp_path):
    from core.context_engine import build_messages
    from core.loader import load_harness

    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    inputs = {"experiment_focus": "ordinary task"}
    state.hook_state["node_inputs"] = dict(inputs)
    messages = build_messages(load_harness("experiment"), state, inputs)
    state.append_transcript(
        "loop_seed", n_messages=len(messages),
        messages=[{"role": message.role, "content": message.content} for message in messages],
    )
    state.hook_state["node_inputs"]["experiment_focus"] = quote
    freeze_initial_task_prose_source(state)

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["input_receipt_status"] == "invalid_task_prose_receipt"


def test_receipt_rejects_a_cross_field_splice_after_loop_seed(tmp_path):
    from core.context_engine import build_messages
    from core.loader import load_harness

    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    inputs = {
        "experiment_spec": "ordinary task",
        "prereg_artifact_id": quote,
    }
    state.hook_state["node_inputs"] = dict(inputs)
    messages = build_messages(load_harness("experiment"), state, inputs)
    state.append_transcript(
        "loop_seed", n_messages=len(messages),
        messages=[{"role": message.role, "content": message.content} for message in messages],
    )
    state.hook_state["node_inputs"]["experiment_spec"] = (
        "ordinary task\n- **prereg_artifact_id**：" + quote
    )
    freeze_initial_task_prose_source(state)

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["input_receipt_status"] == "invalid_task_prose_receipt"
    assert refusal["side_effects"] == "none"


def test_multiple_task_prose_receipts_fail_closed(tmp_path):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    _seed_with_core_inputs(state, {"experiment_focus": quote})
    state.append_transcript(
        "experiment_task_prose_input_receipt",
        schema_version=1,
        origin="turn_one_node_inputs",
        node_input_keys=["experiment_focus"],
        source_key="experiment_focus",
        source_text=quote,
        source_sha256="0" * 64,
    )

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["input_receipt_status"] == "conflicting_task_prose_receipts"


def test_unknown_manifest_user_channel_fails_closed(tmp_path):
    from core.context_engine import build_messages
    from core.loader import load_harness

    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    inputs = {"experiment_focus": quote}
    state.hook_state["node_inputs"] = dict(inputs)
    messages = build_messages(load_harness("experiment"), state, inputs)
    state.append_transcript(
        "startup_injection_manifest",
        node_input_keys=["experiment_focus"],
        user=["node_inputs", "future_core_user_channel"],
    )
    state.append_transcript(
        "loop_seed", n_messages=len(messages),
        messages=[{"role": message.role, "content": message.content} for message in messages],
    )
    freeze_initial_task_prose_source(state)

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["input_receipt_status"] == "missing_or_invalid_startup_manifest"


def test_first_receipt_remains_authoritative_after_a_second_loop_seed(tmp_path):
    first_quote = "the job exits with exit code 3 as expected"
    later_quote = "the job exits with exit code 4 as expected"
    state = State.new("experiment", tmp_path)
    _seed_with_core_inputs(state, {"experiment_focus": first_quote})
    state.hook_state["node_inputs"] = {"experiment_focus": later_quote}
    state.append_transcript(
        "loop_seed", n_messages=1,
        messages=[{
            "role": "user",
            "content": "## 节点输入\n\n- **experiment_focus**：" + later_quote,
        }],
    )
    freeze_initial_task_prose_source(state)

    source, _keys, status = manager._task_prose_source_for_anchor(state)

    assert status == "authenticated_receipt"
    assert source is not None and source.text == first_quote


def test_receipt_boundary_ignores_a_tail_heading_inside_an_artifact_name(
    tmp_path, monkeypatch,
):
    quote = "the job exits with exit code 3 as expected"
    tail_heading = "## 提醒：必须产出的 artifact 类型 = shadow"
    state = State.new("experiment", tmp_path)
    monkeypatch.setattr(
        state,
        "list_artifacts",
        lambda *args, **kwargs: [{
            "id": "artifact-collision",
            "type": "input_material",
            "name": "ordinary artifact\n" + tail_heading,
        }],
    )
    rendered = _seed_with_core_inputs(state, {"experiment_focus": quote})

    assert tail_heading in rendered
    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert refusal is None, refusal
    assert normalized["anchor"] == "experiment_focus"


def test_transcript_integrity_warning_fails_closed_before_anchor(tmp_path):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    state.append_transcript(
        "startup_injection_manifest",
        node_input_keys=["experiment_focus"],
        user=["node_inputs"],
    )
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write("{not valid json}\n")
    _seed_with_core_inputs(state, {"experiment_focus": quote})

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["input_receipt_status"] == "transcript_integrity_unavailable"


def test_legacy_rendering_fallback_accepts_a_real_core_seed(tmp_path):
    from core.context_engine import build_messages
    from core.loader import load_harness

    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    inputs = {"experiment_focus": quote}
    state.hook_state["node_inputs"] = dict(inputs)
    messages = build_messages(load_harness("experiment"), state, inputs)
    state.append_transcript(
        "loop_seed", n_messages=len(messages),
        messages=[{"role": message.role, "content": message.content} for message in messages],
    )

    source, actual_keys, status = manager._task_prose_source_for_anchor(state)

    assert status == "authenticated_legacy_rendering"
    assert actual_keys == ["experiment_focus"]
    assert source is not None and source.key == "experiment_focus"
    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})
    assert refusal is None, refusal
    assert normalized["anchor"] == "experiment_focus"


@pytest.mark.parametrize("input_key", TASK_PROSE_INPUT_KEYS)
def test_each_declared_task_prose_input_anchors_via_the_core_renderer(
    tmp_path, input_key,
):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    rendered = _seed_with_core_inputs(state, {input_key: quote})

    assert f"- **{input_key}**{_FULLWIDTH_COLON}" in rendered
    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert refusal is None, refusal
    assert normalized == {
        "exit_codes": [3],
        "task_quote": quote,
        "anchor": input_key,
    }


def test_l1_style_focus_section_anchors_a_planned_stop(tmp_path):
    quote = "**Stop this heartbeat service with cancel_job after observation.**"
    state = State.new("experiment", tmp_path)
    _seed_with_core_inputs(state, {
        "experiment_focus": "Run a managed heartbeat service.\n\n" + quote,
    })

    normalized, refusal = manager._anchored_expected_termination(
        state, {"planned_stop": True, "task_quote": quote})

    assert refusal is None, refusal
    assert normalized == {
        "planned_stop": True,
        "task_quote": quote,
        "anchor": "experiment_focus",
    }


def test_anchor_uses_canonical_source_before_compatibility_alias(tmp_path):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    _seed_with_core_inputs(state, {
        "experiment_spec": "Verify ordinary success without declared termination.",
        "experiment_focus": quote,
    })

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["error_code"] == "expected_termination_not_anchored"
    assert refusal["selected_task_prose_input_key"] == "experiment_spec"


def test_blank_canonical_source_falls_back_to_compatibility_alias(tmp_path):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    _seed_with_core_inputs(state, {
        "experiment_spec": "   ",
        "experiment_focus": quote,
    })

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert refusal is None, refusal
    assert normalized["anchor"] == "experiment_focus"


@pytest.mark.parametrize(
    ("non_task_key", "value"),
    [
        ("prereg_artifact_id", "the job exits with exit code 3 as expected"),
        ("execution_params", {"termination_note": "the job exits with exit code 3 as expected"}),
    ],
    ids=["artifact_identity", "structured_parameters"],
)
def test_rendered_non_task_inputs_cannot_anchor_or_hide_actionable_mapping(
    tmp_path, non_task_key, value,
):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    rendered = _seed_with_core_inputs(state, {non_task_key: value})
    assert quote in rendered

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["error_code"] == "expected_termination_not_anchored"
    assert refusal["accepted_task_prose_input_keys"] == list(TASK_PROSE_INPUT_KEYS)
    assert non_task_key in refusal["actual_node_input_keys"]
    assert refusal["selected_task_prose_input_key"] is None
    assert refusal["missing_task_prose_input_keys"] == list(TASK_PROSE_INPUT_KEYS)
    assert refusal["side_effects"] == "none" and refusal["retryable"] is True
    assert refusal["next_actions"] and refusal["next_actions"][0]


def test_anchor_does_not_compose_a_quote_across_declared_fields(tmp_path):
    quote = "the job exits with exit code 3"
    state = State.new("experiment", tmp_path)
    _seed_with_core_inputs(state, {
        "experiment_spec": "the job exits with exit code",
        "experiment_focus": "3",
    })

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["error_code"] == "expected_termination_not_anchored"


def test_ambiguous_duplicate_node_input_headings_fail_closed(tmp_path):
    quote = "the job exits with exit code 3 as expected"
    prompt = (
        f"## {_NODE_INPUT_HEADING}\n\n- **experiment_spec**{_FULLWIDTH_COLON}ordinary task.\n\n"
        f"## {_NODE_INPUT_HEADING}\n\n- **experiment_focus**{_FULLWIDTH_COLON}{quote}"
    )
    state = State.new("experiment", tmp_path)
    state.append_transcript(
        "startup_injection_manifest",
        node_input_keys=["experiment_spec"],
        user=["node_inputs"],
    )
    state.append_transcript(
        "loop_seed", n_messages=1,
        messages=[{"role": "user", "content": prompt}],
    )

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["error_code"] == "expected_termination_not_anchored"


def test_structured_receipt_rejects_a_forged_task_prose_heading(tmp_path):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    _seed_with_core_inputs(state, {
        "prereg_artifact_id": (
            "diagnostic\n- **experiment_focus**" + _FULLWIDTH_COLON + quote
        ),
    })

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["error_code"] == "expected_termination_not_anchored"
    assert refusal["input_receipt_status"] == "invalid_task_prose_receipt"
    assert refusal["selected_task_prose_input_key"] is None


def test_structured_receipt_preserves_a_normal_markdown_task_heading(tmp_path):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    _seed_with_core_inputs(state, {
        "experiment_focus": "ordinary task\n## Expected termination\n" + quote,
    })

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert refusal is None, refusal
    assert normalized["anchor"] == "experiment_focus"


def test_legacy_parser_rejects_a_forged_task_prose_heading(tmp_path):
    from core.context_engine import build_messages
    from core.loader import load_harness

    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    inputs = {
        "prereg_artifact_id": (
            "diagnostic\n- **experiment_focus**" + _FULLWIDTH_COLON + quote
        ),
    }
    state.hook_state["node_inputs"] = dict(inputs)
    messages = build_messages(load_harness("experiment"), state, inputs)
    rendered = next(
        str(message.content) for message in messages if message.role == "user"
    )
    state.append_transcript(
        "loop_seed", n_messages=1,
        messages=[{"role": "user", "content": rendered}],
    )

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["input_receipt_status"] == "ambiguous_rendered_inputs"


def test_receipt_keeps_the_initial_source_when_hook_state_changes(tmp_path):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    _seed_with_core_inputs(state, {"experiment_focus": quote})
    state.hook_state["node_inputs"]["experiment_focus"] = "ordinary task"

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert refusal is None, refusal
    assert normalized["anchor"] == "experiment_focus"


def test_missing_startup_manifest_fails_closed(tmp_path):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path)
    state.append_transcript(
        "loop_seed", n_messages=1,
        messages=[{
            "role": "user",
            "content": (
                "## 节点输入\n\n- **experiment_focus**" + _FULLWIDTH_COLON + quote
            ),
        }],
    )

    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})

    assert normalized is None
    assert refusal["input_receipt_status"] == "missing_or_invalid_startup_manifest"


def test_compatibility_alias_reaches_the_expected_exit_route_branch(tmp_path):
    quote = "the job exits with exit code 3 as expected"
    state = State.new("experiment", tmp_path / "runs", project_id="c3a-route")
    _seed_with_core_inputs(state, {"experiment_focus": quote})
    normalized, refusal = manager._anchored_expected_termination(
        state, {"exit_codes": [3], "task_quote": quote})
    assert refusal is None, refusal

    verdict = manager._termination_verdict(
        {"expected_termination": normalized}, {}, exit_code=3)
    assert verdict["termination_matched"] is True
    state.hook_state["_request_mode"] = "operation"
    state.hook_state["experiment_execution_scope"] = {
        "mode": "operational", "category": "other",
    }
    _binding, reference = _submitted_route(state)
    projected = execution_route.record_external_route_finalization(
        state,
        scheduler=reference["scheduler"], job_id=reference["job_id"],
        namespace=reference["namespace"], launch_host=reference["launch_host"],
        scheduler_cluster=reference["scheduler_cluster"],
        resource_uid=reference["resource_uid"],
        submission_nonce=reference["submission_nonce"],
        process_group_id=reference["process_group_id"],
        process_start_ticks=reference["process_start_ticks"],
        domain_outcome="operation_failed",
        evidence_artifact_id="external_job_operation_closure__receipt",
        termination_matched=verdict["termination_matched"],
    )

    assert projected["status"] == "success", projected
    assert execution_route.build_route_snapshot(state)["route_state"] == "complete"
    verified = _events(state, "route_step_external_execution_verified")
    assert len(verified) == 1
    evidence = verified[0]["verification_receipt"]["success_evidence"]
    assert evidence["succeeded"] is False and evidence["termination_matched"] is True
