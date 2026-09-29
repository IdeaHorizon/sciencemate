"""门禁判据的证据源：工具自己的 receipt，而不是会被截断的 tool_result preview。

核心回归（test_truncated_declaration_result_still_audits）锁的是一个实测死锁：
`declare_inconclusive_verdict` 成功返回，但结果 JSON 超过 500 字符，被
core/agent_loop.py 的 _brief 压成解析不了的字符串，审计据此判定"没声明过"，
于是 experiment_log 永远冻不了。reason/next_step 写得越详细越容易触发 —— 门禁
惩罚了它本想奖励的行为。见 fresh-07-slurm-resume run 1787052091-0f9f9f。
"""
import json
from pathlib import Path

from core.state import State
from nodes.experiment.tools.contract_audit import (
    _unreadable_tool_results,
    _valid_inconclusive_declarations,
    audit_sediment,
    audit_verdict,
)

_REASON = (
    "frozen prereg 要求 scheduler=slurm 且 requires_scheduler=true，但本环境完全不存在 SLURM："
    "sbatch/squeue/sinfo/scancel/srun 二进制全部缺失，/etc/slurm 与 /var/spool/slurm 均不存在，"
    "唯一可用 scheduler 为 local，因此 checkpoint/interrupt/resume 协议无法在真实队列上执行。"
)
_NEXT_STEP = (
    "(a) 由平台或编排补一个带 debug 队列的 SLURM 环境后重跑两段式协议，并保留两个 job ID、"
    "提交脚本、输出路径、checkpoint hash 与最终序列号；或 (b) 由 hypothesis 节点修订冻结预注册，"
    "显式声明可替代的调度器后重新走正式执行路径。"
)


def _primary_state(tmp_path: Path) -> State:
    state = State.new("experiment", tmp_path)
    state.hook_state["run_contract"] = {
        "run_role": "primary",
        "stage": "simulation",
        "analysis_eligible": True,
    }
    return state


def _log(state: State, *, name: str = "slurm_log", auto: bool = False,
         body: str = "## Execution Status\nstatus: blocked\n") -> str:
    metadata = {"auto_generated": True} if auto else None
    return state.save_artifact("experiment_log", name, body, metadata=metadata)["id"]


def _declare_receipt(state: State, log_id: str, *, frozen: bool = False,
                     reason: str = _REASON, next_step: str = _NEXT_STEP) -> None:
    state.append_transcript(
        "experiment_inconclusive_verdict_declared",
        experiment_log_id=log_id, reason=reason, next_step=next_step,
        log_frozen=frozen, rendered_in_log=not frozen,
    )


def _declare_tool_call(state: State, log_id: str, *, truncated: bool) -> None:
    """回放 agent_loop 写 transcript 的两种形态：完整 dict，或 _brief 截断后的字符串。"""
    state.append_transcript(
        "tool_call", name="declare_inconclusive_verdict",
        args={"experiment_log_id": log_id, "reason": _REASON, "next_step": _NEXT_STEP},
    )
    result = {"status": "success", "experiment_log_id": log_id, "log_frozen": False,
              "rendered_in_log": True, "reason": _REASON, "next_step": _NEXT_STEP}
    text = json.dumps(result, ensure_ascii=False)
    if truncated:
        assert len(text) > 500, "本用例要求结果确实超过 _brief 的 500 字符阈值"
        preview = text[:500] + "...[truncated]"
    else:
        preview = result
    state.append_transcript("tool_result", name="declare_inconclusive_verdict",
                            result_preview=preview)


# ── G：回归锚点 —— 截断不再让声明消失 ─────────────────────────────
def test_truncated_declaration_result_still_audits(tmp_path: Path):
    state = _primary_state(tmp_path)
    log_id = _log(state)
    _declare_receipt(state, log_id)
    _declare_tool_call(state, log_id, truncated=True)

    assert _unreadable_tool_results(state) == {"declare_inconclusive_verdict": 1}
    verdict = audit_verdict(state)
    assert verdict["passed"] is True
    assert verdict["has_explicit_declaration"] is True


# ── F：回落 —— 没有 receipt 的历史 transcript 行为不变 ────────────
def test_legacy_transcript_without_receipt_still_audits(tmp_path: Path):
    state = _primary_state(tmp_path)
    log_id = _log(state)
    _declare_tool_call(state, log_id, truncated=False)

    assert audit_verdict(state)["passed"] is True


# ── A/B：冻结前渲染进正文，冻结后只留 addendum ────────────────────
def test_pre_and_post_freeze_declarations_both_count(tmp_path: Path):
    pre = _primary_state(tmp_path / "pre")
    pre_log = _log(pre)
    _declare_receipt(pre, pre_log, frozen=False)
    assert audit_verdict(pre)["passed"] is True

    post = _primary_state(tmp_path / "post")
    post_log = _log(post)
    _declare_receipt(post, post_log, frozen=True)
    assert audit_verdict(post)["passed"] is True


# ── C：一个 run 内多次声明全部收集 ────────────────────────────────
def test_multiple_declarations_are_all_collected(tmp_path: Path):
    state = _primary_state(tmp_path)
    log_id = _log(state)
    _declare_receipt(state, log_id, frozen=False)
    _declare_receipt(state, log_id, frozen=True)

    declarations = _valid_inconclusive_declarations(state, {"id": log_id, "metadata": {}})
    assert len(declarations) == 2
    assert any(item["log_frozen"] for item in declarations)


# ── D：声明绑定的不是当前最新 log → 不计入 ────────────────────────
def test_declaration_bound_to_other_log_is_ignored(tmp_path: Path):
    state = _primary_state(tmp_path)
    _log(state)
    _declare_receipt(state, "experiment_log__some_other_log")

    assert audit_verdict(state)["passed"] is False


# ── D2：reason/next_step 过短 → 不计入（工具阈值漂移的保险）────────
def test_short_reason_receipt_is_rejected(tmp_path: Path):
    state = _primary_state(tmp_path)
    log_id = _log(state)
    _declare_receipt(state, log_id, reason="太短", next_step="也短")

    assert audit_verdict(state)["passed"] is False


# ── E：自动兜底日志不能形成 verdict ───────────────────────────────
def test_auto_generated_log_cannot_be_closed_by_receipt(tmp_path: Path):
    state = _primary_state(tmp_path)
    log_id = _log(state, auto=True)
    _declare_receipt(state, log_id)

    verdict = audit_verdict(state)
    assert verdict["passed"] is False
    assert verdict["auto_generated_record"] is True


# ── P2：拒绝可以，但必须说清拒的是"读不到"还是"没发生" ────────────
def test_failure_reason_names_the_unreadable_tool(tmp_path: Path):
    state = _primary_state(tmp_path)
    log_id = _log(state)
    _declare_tool_call(state, log_id, truncated=True)   # 只有截断记录，没有 receipt

    verdict = audit_verdict(state)
    assert verdict["passed"] is False
    assert "declare_inconclusive_verdict" in verdict["reason"]
    assert "截断" in verdict["reason"]


# ── sediment 门禁：同样的两条 ─────────────────────────────────────
def test_sediment_receipt_survives_truncation(tmp_path: Path):
    state = _primary_state(tmp_path)
    log_id = _log(state)
    state.append_transcript(
        "experiment_no_sediment_declared",
        experiment_log_id=log_id, reason=_REASON, log_frozen=False, rendered_in_log=True,
    )
    state.append_transcript("tool_call", name="declare_no_sediment", args={"reason": _REASON})
    state.append_transcript("tool_result", name="declare_no_sediment",
                            result_preview='{"status": "success", "experiment_log_id"...[truncated]')

    sediment = audit_sediment(state)
    assert sediment["passed"] is True
    assert sediment["has_explicit_declaration"] is True


def test_sediment_legacy_tool_result_still_counts(tmp_path: Path):
    state = _primary_state(tmp_path)
    log_id = _log(state)
    state.append_transcript("tool_call", name="declare_no_sediment", args={"reason": _REASON})
    state.append_transcript(
        "tool_result", name="declare_no_sediment",
        result_preview={"status": "success", "experiment_log_id": log_id,
                        "log_frozen": False, "rendered_in_log": True},
    )

    assert audit_sediment(state)["passed"] is True


# ─────────────────────────────────────────────────────────────────────
# KB experiment record 是可选索引：无论 blocked 或已冻结结果，未登记都不影响执行证据闭环
# ─────────────────────────────────────────────────────────────────────
from nodes.experiment.tools.contract_audit import audit_execution_record


def _report_blocker(state: State, *, evidence: bool = True) -> None:
    state.append_transcript(
        "blocker_reported", blocker_id=f"{state.run_id}:1", reporting_node="experiment",
        category="environment", summary="SLURM 在本环境物理不存在，primary simulation 无法执行。",
        evidence_paths=["/run/outputs/evidence_slurm_absence.txt"] if evidence else [],
        retryable_after_change=True,
    )


def test_blocked_run_with_evidence_does_not_require_execution_record(tmp_path: Path):
    state = _primary_state(tmp_path)
    _log(state)
    _report_blocker(state)

    record = audit_execution_record(state)
    assert record["passed"] is True
    assert record["applicable"] is False
    assert record["registration_status"] == "not_registered"
    assert record["required"] is False


def test_blocker_without_evidence_still_requires_execution_record(tmp_path: Path):
    """不可行性要有正证据，否则'跑不动就宣布 blocked'成了逃避收尾的后门。"""
    state = _primary_state(tmp_path)
    _log(state)
    _report_blocker(state, evidence=False)

    assert audit_execution_record(state)["passed"] is True
    assert audit_execution_record(state)["registration_status"] == "not_registered"


def test_frozen_clean_results_still_require_execution_record(tmp_path: Path):
    """真跑出并冻结了科学结果，不因末尾撞上一个 blocker 就免除登记。"""
    state = _primary_state(tmp_path)
    _log(state)
    saved = state.save_artifact("clean_results", "measured", '{"value": 1}')
    state.mark_frozen(saved["id"])   # 冻结只出自账本的 freeze 行
    _report_blocker(state)

    assert audit_execution_record(state)["passed"] is True
    assert audit_execution_record(state)["registration_status"] == "not_registered"


def test_framework_closure_blocker_does_not_trigger_exemption(tmp_path: Path):
    """hook 自产的 experiment_closure_incomplete 不走 report_blocker，不得被当成豁免依据。"""
    state = _primary_state(tmp_path)
    _log(state)
    state.hook_state.setdefault("blockers", []).append(
        {"blocker_id": "experiment_closure_incomplete", "category": "closure"})

    assert audit_execution_record(state)["passed"] is True
    assert audit_execution_record(state)["registration_status"] == "not_registered"


def test_verdict_failure_names_the_three_legal_exits(tmp_path: Path):
    state = _primary_state(tmp_path)
    _log(state)

    reason = audit_verdict(state)["reason"]
    assert "declare_inconclusive_verdict" in reason
    assert "provisional" in reason and "inconclusive" in reason
    assert "Analysis" in reason        # 说明它不是科学裁决
