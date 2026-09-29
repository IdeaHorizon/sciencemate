"""issue #426：框架门禁造成的失败不得计入节点卡死统计。

nidy2 实测形状：experiment 连续 6 次 missing:clean_results，每一次都死在
submit_job 拒路径 ↔ safe_run_bash 要求走 submit_job 的门禁死锁上。旧逻辑把
这 6 次算成节点连败 → 第 4 次起永久拒绝派发，环境修好后节点仍起不来。

三层各测各的：
  1. executor._gate_block_evidence      —— 机械证据判定（纯函数）
  2. executor._classify_incomplete_failure_detail —— 归类
  3. run_history.consecutive_failures   —— 卡死统计剔除外因失败
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.agent_loop import LoopResult
from core.executor import (
    FAILURE_CATEGORY_FRAMEWORK_GATE,
    FAILURE_CATEGORY_PROTOCOL,
    _classify_incomplete_failure_detail,
    _gate_block_evidence,
)
from core.run_history import (
    EXTERNAL_FAILURE_CATEGORIES,
    RunRecord,
    consecutive_failures,
)


# ── 记录构造 helpers ────────────────────────────────────────────────────────

def _gate_rec(kind: str, tool: str = "submit_job") -> dict:
    """一条被机械门禁拒绝的 all_tool_calls 记录（结构化 blocker 契约）。"""
    return {
        "name": tool,
        "args": {},
        "result": {"status": "error", "error": "⛔ ...", "blocker": {"kind": kind}},
    }


def _ok_rec(tool: str = "write_scratchpad") -> dict:
    return {"name": tool, "args": {}, "result": {"status": "success"}}


def _err_rec(tool: str = "safe_run_bash") -> dict:
    """普通业务失败：error 但没有结构化 blocker —— 不是门禁。"""
    return {"name": tool, "args": {}, "result": {"status": "error", "error": "exit 1"}}


# ── 1. _gate_block_evidence ────────────────────────────────────────────────

def test_terminal_gate_block_detected():
    """run 终结在门上（最后一条记录就是门禁拒绝）。"""
    records = [_ok_rec(), _gate_rec("managed_submission_required", "safe_run_bash")]
    ev = _gate_block_evidence(records)
    assert ev is not None
    assert ev["kind"] == "managed_submission_required"
    assert ev["tool"] == "safe_run_bash"


def test_persistent_same_kind_blocks_detected():
    """同类门禁拒绝 ≥3 次，即使收尾是无关成功调用（写 scratchpad 存报告）。"""
    records = [
        _gate_rec("managed_submission_required"),
        _ok_rec(),
        _gate_rec("managed_submission_required"),
        _ok_rec("safe_run_bash"),  # 短命令成功 —— 不能洗掉长任务的门禁循环
        _gate_rec("managed_submission_required"),
        _ok_rec(),  # 收尾：保存诊断报告
    ]
    ev = _gate_block_evidence(records)
    assert ev is not None
    assert ev["kind"] == "managed_submission_required"
    assert ev["count"] == 3


def test_one_or_two_bumps_then_normal_work_is_not_gate_blocked():
    """撞一两次门后正常干活 = 门在正常引导，不算门禁失败。"""
    records = [
        _gate_rec("scope_guard"),
        _ok_rec("safe_run_bash"),
        _gate_rec("unmanaged_background_launch"),
        _ok_rec("save_artifact"),
    ]
    assert _gate_block_evidence(records) is None


def test_plain_errors_are_not_gate_blocks():
    """普通业务 error（无结构化 blocker）永远不算门禁。"""
    records = [_err_rec(), _err_rec(), _err_rec(), _err_rec()]
    assert _gate_block_evidence(records) is None


def test_report_blocker_success_result_is_not_a_gate_block():
    """report_blocker 是 agent 主动申报（status=success 且带 blocker 字段），
    与机械门禁拒绝（status=error）不能混淆。"""
    records = [
        {
            "name": "report_blocker",
            "args": {},
            "result": {"status": "success", "blocker": {"category": "environment"}},
        }
    ] * 4
    assert _gate_block_evidence(records) is None


def test_empty_records():
    assert _gate_block_evidence([]) is None


# ── 2. 分类 ────────────────────────────────────────────────────────────────

def test_classify_terminal_gate_block():
    lr = LoopResult(
        final_text="路径反复被拒，无法提交作业。",
        turns=40,
        tool_calls=[_ok_rec(), _gate_rec("managed_submission_required")],
    )
    cat, sub = _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["clean_results"]
    )
    assert cat == FAILURE_CATEGORY_FRAMEWORK_GATE
    assert sub == "managed_submission_required"


def test_classify_requires_missing_outputs():
    """产出齐了只是别的原因 incomplete → 不归门禁账（与 malformed 同防线）。"""
    lr = LoopResult(
        final_text="", turns=10,
        tool_calls=[_gate_rec("scope_guard")] * 4,
    )
    cat, _ = _classify_incomplete_failure_detail(lr, missing_required_outputs=[])
    assert cat is None


def test_classify_genuine_node_failure_untouched():
    """正常自我纠正（早期撞一次门，之后成功干活）→ 仍是节点质量归因。"""
    lr = LoopResult(
        final_text="", turns=20,
        tool_calls=[_gate_rec("scope_guard"), _ok_rec("safe_run_bash"),
                    _ok_rec("save_artifact")],
    )
    cat, _ = _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["clean_results"]
    )
    assert cat is None


def test_classify_protocol_still_wins_on_malformed_terminal_record():
    """终结在 args 解析失败上 → 归协议账（既有防线优先级不变）。"""
    lr = LoopResult(
        final_text="", turns=5,
        tool_calls=[
            _gate_rec("scope_guard"),
            {"name": "save_artifact", "args": {},
             "result": {"status": "error",
                        "error": "参数 JSON 解析失败：Unterminated string"}},
        ],
    )
    cat, sub = _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["clean_results"]
    )
    assert cat == FAILURE_CATEGORY_PROTOCOL
    assert sub == "malformed_tool_args"


# ── 3. 卡死统计 ────────────────────────────────────────────────────────────

def _run(run_id: str, *, category: str | None = None,
         raw: dict | None = None, completed: bool = False) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        state_dir=Path("/nonexistent") / run_id,
        node_type="experiment",
        project_id="p1",
        status="completed" if completed else "incomplete",
        missing_required_outputs=() if completed else ("clean_results",),
        failure_category=category,
        raw=raw or {},
    )


def test_nidy2_replay_gate_blocked_runs_do_not_lock_the_node():
    """6 次连续 missing:clean_results、全部死在门禁上 → 熔断不触发。"""
    runs = [
        _run(f"178650000{i}-aaaaa{i}", category=FAILURE_CATEGORY_FRAMEWORK_GATE)
        for i in range(6)
    ]
    assert consecutive_failures(runs, "experiment") is None


def test_evidence_alone_excludes_even_without_category():
    """判决现算：老分类器没打类别，但 gate_block_evidence 证据在 → 照样剔除。"""
    runs = [
        _run(f"178650001{i}-bbbbb{i}",
             raw={"gate_block_evidence": {"kind": "managed_submission_required",
                                          "tool": "safe_run_bash", "count": 5}})
        for i in range(4)
    ]
    assert consecutive_failures(runs, "experiment") is None


def test_genuine_failures_still_counted():
    """真实的节点连败（无外因证据）→ 熔断照常工作。"""
    runs = [_run(f"178650002{i}-ccccc{i}") for i in range(4)]
    hit = consecutive_failures(runs, "experiment")
    assert hit is not None
    assert hit["count"] == 4
    assert "missing:clean_results" in hit["signals"]


def test_mixed_only_genuine_count():
    """门禁失败夹在真实失败中间：只数真实的，且不断链。"""
    runs = [
        _run("1786500031-ddddd1"),
        _run("1786500032-ddddd2", category=FAILURE_CATEGORY_FRAMEWORK_GATE),
        _run("1786500033-ddddd3"),
        _run("1786500034-ddddd4", category=FAILURE_CATEGORY_FRAMEWORK_GATE),
        _run("1786500035-ddddd5"),
    ]
    hit = consecutive_failures(runs, "experiment")
    assert hit is not None
    assert hit["count"] == 3  # 只有 3 次真实失败


def test_old_summaries_stay_conservative():
    """老 summary（无类别无证据）→ 维持旧行为，照常计数。"""
    runs = [_run(f"178650004{i}-eeeee{i}") for i in range(3)]
    hit = consecutive_failures(runs, "experiment")
    assert hit is not None and hit["count"] == 3


def test_completed_run_still_breaks_the_chain():
    runs = [
        _run("1786500051-fffff1"),
        _run("1786500052-fffff2", completed=True),
        _run("1786500053-fffff3"),
    ]
    hit = consecutive_failures(runs, "experiment")
    assert hit is None  # 最新一次失败后紧跟成功 → 链长 1，不足 2


def test_external_categories_single_truth_source():
    """两个类别都在唯一真相源里（消费方不再各自维护名单）。"""
    assert "provider_tool_call_protocol_error" in EXTERNAL_FAILURE_CATEGORIES
    assert FAILURE_CATEGORY_FRAMEWORK_GATE in EXTERNAL_FAILURE_CATEGORIES
