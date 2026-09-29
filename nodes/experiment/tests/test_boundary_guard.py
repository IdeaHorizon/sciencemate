"""Experiment shell 必须保留 framework 的不可 bypass 边界拒绝。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.state import State
from nodes.experiment.tools import safe_bash as sb
from shared.lib import dangerous_commands as danger


def _events(state: State) -> list[dict]:
    if not state.transcript_path.exists():
        return []
    return [json.loads(line) for line in state.transcript_path.read_text().splitlines()
            if line.strip()]


def _assert_hard_boundary_deny(state: State, result: dict) -> None:
    assert result["status"] == "error"
    assert "越界写入拦截" in result["error"]
    events = _events(state)
    assert any(event.get("event") == "boundary_write_blocked"
               and event.get("tool") == "safe_run_bash" for event in events)
    assert not any(event.get("event", "").endswith("_bypassed") for event in events)


def test_framework_state_write_is_boundary_denied_before_scope_guard(tmp_path):
    state = State.new("experiment", tmp_path)
    target = state.root / "artifacts" / "must-not-exist.json"

    result = asyncio.run(sb._safe_run_bash(state, "echo x > artifacts/must-not-exist.json"))

    _assert_hard_boundary_deny(state, result)
    assert not target.exists()


def test_boundary_deny_is_not_bypassed(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path)
    target = state.root / "artifacts" / "must-not-exist.json"
    monkeypatch.setattr(danger, "BYPASS_ENABLED", True)
    try:
        result = asyncio.run(
            sb._safe_run_bash(state, "echo x > artifacts/must-not-exist.json"))
    finally:
        monkeypatch.setattr(danger, "BYPASS_ENABLED", False)

    _assert_hard_boundary_deny(state, result)
    assert not target.exists()


def test_nohup_is_no_longer_refused_for_being_nohup(tmp_path):
    """判决拆除·第三波（sb:3631 降格）：nohup/& 那道「预测会孤儿化」的墙已删。

    合并说明（本分支 vs origin/main）：上游那版断言 nohup 照跑并带
    background_launch_dies_with_shell 见证；在受管生命周期下 safe_run_bash
    根本不允许承担 process_tree —— 冻结路线的 schema 明写「process_tree 必须由
    submit_job 承担」，所以后台化命令仍然被拒，但**理由换了人**：不是"我预测
    它会孤儿化"，而是"这个工具没有持久身份，去用 submit_job"。

    这里锁住的正是那个差别：旧墙（unmanaged_background_launch / 对应 transcript
    事件）加回去即转红；submit_job 一侧的照跑+见证由
    test_resource_manager.test_backgrounded_job_command_is_submitted_and_witnessed 守。
    """
    state = State.new("experiment", tmp_path)
    cmd = "nohup sleep 30 > /tmp/harness-bg.log 2>&1 &"

    result = asyncio.run(sb._safe_run_bash(state, cmd))

    assert (result.get("blocker") or {}).get("kind") != "unmanaged_background_launch"
    assert "nohup" not in str(result.get("error") or "")
    events = _events(state)
    assert not any(event.get("event") == "unmanaged_background_launch_blocked"
                   for event in events)


def test_missing_bash_analyzer_is_an_honest_framework_blocker(tmp_path, monkeypatch):
    """The node must not tell an experiment to repair the host runtime."""
    state = State.new("experiment", tmp_path)
    monkeypatch.setattr(sb._te, "bash_analyzer_unavailable_reason",
                        lambda: "ModuleNotFoundError: tree_sitter_bash")

    result = asyncio.run(sb._safe_run_bash(state, "echo probe"))

    assert result["status"] == "error"
    assert result["blocker"]["kind"] == "bash_semantic_analyzer_unavailable"
    assert result["blocker"]["suggested_owner"] == "framework"
    assert result["blocker"]["node_action"] == "report_blocker_do_not_modify_global_environment"
    assert "不得安装或修改全局 Python 环境" in result["error"]

def test_undeclared_absolute_write_is_not_bypassed(tmp_path, monkeypatch):
    """Bypass skips confirmation, never changes the run's declared route."""
    state = State.new("experiment", tmp_path)
    outside = Path("/var/lib/harness-test-undeclared") / "must-not-exist.txt"
    monkeypatch.setattr(danger, "BYPASS_ENABLED", True)
    try:
        result = asyncio.run(
            sb._safe_run_bash(state, f"touch {outside}"))
    finally:
        monkeypatch.setattr(danger, "BYPASS_ENABLED", False)

    assert result["status"] == "error"
    assert not outside.exists()


def test_safe_run_bash_rejects_conflicting_path_roles(tmp_path):
    """A conflicting baseline/build contract must not be writable through Bash."""
    state = State.new("experiment", tmp_path)
    run_root = tmp_path / "run"
    conflict = tmp_path / "conflict"
    run_root.mkdir()
    conflict.mkdir()
    target = conflict / "must-not-exist.txt"
    state.hook_state["path_roles"] = {
        "run_root": str(run_root),
        "source_baseline_root": str(conflict),
        "build_root": str(conflict),
    }

    result = asyncio.run(sb._safe_run_bash(
        state, f"printf blocked > {target}", cwd=str(run_root)))

    assert result["status"] == "error"
    assert not target.exists()


def test_safe_run_bash_cannot_write_immutable_baseline(tmp_path):
    """The public shell entrypoint must honor projected readonly roles."""
    state = State.new("experiment", tmp_path)
    run_root = tmp_path / "run"
    baseline = tmp_path / "baseline"
    run_root.mkdir()
    baseline.mkdir()
    target = baseline / "must-not-exist.txt"
    state.hook_state["path_roles"] = {
        "run_root": str(run_root),
        "build_root": str(tmp_path / "build"),
        "source_baseline_root": str(baseline),
    }

    result = asyncio.run(sb._safe_run_bash(
        state, f"printf blocked > {target}", cwd=str(run_root)))

    assert result["status"] == "error"
    assert not target.exists()


def test_pytest_bash_analyzer_import_identity_is_diagnosable():
    """Duplicate or stale imports fail with the exact module identities."""
    import sys
    from nodes.experiment.tools import timeout_escalation as te

    semantics = sys.modules[te.analyze_bash.__module__]
    snapshot = {
        "pytest_executable": sys.executable,
        "safe_bash_timeout_file": getattr(sb._te, "__file__", None),
        "expected_timeout_file": getattr(te, "__file__", None),
        "bash_semantics_file": getattr(semantics, "__file__", None),
        "parser_present": getattr(semantics, "_PARSER", None) is not None,
        "analyzer_unavailable": getattr(semantics, "_ANALYZER_UNAVAILABLE", None),
    }
    assert sb._te is te, snapshot
    assert snapshot["parser_present"], snapshot
    assert snapshot["analyzer_unavailable"] is None, snapshot


def test_frozen_operation_evidence_paths_are_protected_from_deletion(tmp_path):
    """operation 收尾冻结的证据路径同样受删除保护（2026-09-11）。

    原先这道过滤还排除 analysis_eligible=False 的记录，而唯一写那个值的地方正是
    operation 收尾（恒 False）—— 等于把最该保护的 operation 证据排除在保护之外。
    现在只问一件事：这份记录冻结了没有。
    """
    state = State.new("experiment", tmp_path)
    evidence = tmp_path / "operation_receipt.txt"
    evidence.write_text("returncode=0\n", encoding="utf-8")
    saved = state.save_artifact(
        "clean_results", "operation_closure",
        json.dumps({"record_kind": "operation", "status": "completed"}),
        metadata={"record_kind": "operation", "source_path": str(evidence)},
    )
    # 冻结的事实只出自账本的 freeze 行（C1）；save 时带 frozen 会被剥掉。
    state.mark_frozen(saved["id"])

    protected = sb._frozen_result_evidence_paths(state)

    assert sb._norm_path(str(evidence)) in protected, protected
