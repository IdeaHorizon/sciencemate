"""报过阻塞、局面没变，就不该再派一次（issue #524 / #395-9）。

现场：2026-08-19 一个 test 里 continuous 连起三个 writing 子 run，前两个都判定
"上游材料版本存在歧义/材料不足"、如实 report_blocker、写了一份材料不足报告。
必需产出齐、writing QC 全过 —— 于是账本上那两次是 **成功**。既有的重复失败熔断
数的是失败（`consecutive_failures` 见到 `is_completed` 第一眼就断链），对这条
路径完全不在场，所以什么都没拦住。

这些测试钉住三件事：
  1. 判据是"局面变没变"，不是"重试了几次"；
  2. 节点**自己**那次产出（材料不足报告）不算局面变化 —— 否则闸每跑一次自废；
  3. 三条解除路径真的能解除，且证据不在场时放行而不是硬拦。
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from core import dispatch_gate, run_history
from core.bootstrap import bootstrap
from core.project_workspace import bind_project_workspace
from core.state import State

bootstrap()


def _worktree(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=root, check=True, env=env)
    return root


def _bound(tmp_path, node_type, root, *, run_id=None):
    state = State(run_id=run_id or f"r-{node_type}", node_type=node_type,
                  root=tmp_path / f"run-{run_id or node_type}")
    bind_project_workspace(state, root)
    return state


BLOCKER = {
    "blocker_id": "r1:1",
    "reporting_node": "writing",
    "category": "missing_input",
    "summary": "上游 experiment 结果与 prereg 版本对不上，无法确定唯一研究链路",
    "requested_action": "让 experiment 重新交一份绑定当前冻结 prereg 的 clean_results",
    "suggested_owner": "experiment",
    "retryable_after_change": True,
}


# ── 1. 指纹的口径 ───────────────────────────────────────────────────────────

def test_own_output_does_not_count_as_a_changed_situation(tmp_path):
    """闸的自废形态：把节点自己的产出算进指纹，它每跑一次就自动解除。

    writing 被卡住时**必然**留下一份材料不足报告 —— 那正是它交付的东西。
    """
    root = _worktree(tmp_path)
    _bound(tmp_path, "experiment", root).save_artifact(
        "clean_results", "run1", "v1", {})

    before = dispatch_gate.upstream_fingerprint(root, "writing")
    _bound(tmp_path, "writing", root).save_artifact(
        "manuscript", "insufficient", "材料不足报告", {})
    after = dispatch_gate.upstream_fingerprint(root, "writing")

    assert before == after


def test_upstream_change_moves_the_fingerprint(tmp_path):
    root = _worktree(tmp_path)
    before = dispatch_gate.upstream_fingerprint(root, "writing")
    _bound(tmp_path, "experiment", root).save_artifact(
        "clean_results", "run1", "v1", {})
    assert dispatch_gate.upstream_fingerprint(root, "writing") != before


def test_new_version_of_the_same_artifact_moves_the_fingerprint(tmp_path):
    """修订也是局面变化 —— 身份不变、版本变了，指纹必须跟着变（PR #500 三原语）。"""
    root = _worktree(tmp_path)
    experiment = _bound(tmp_path, "experiment", root)
    experiment.save_artifact("clean_results", "run1", "v1", {})
    before = dispatch_gate.upstream_fingerprint(root, "writing")
    experiment.save_artifact("clean_results", "run1", "v2 —— 重算过", {})
    assert dispatch_gate.upstream_fingerprint(root, "writing") != before


def test_no_worktree_yields_no_fingerprint(tmp_path):
    """没绑 Project 的 CLI 单跑：算不出来就说算不出来，不返回一个假的常量。"""
    assert dispatch_gate.upstream_fingerprint(None, "writing") is None


# ── 2. 判据 ────────────────────────────────────────────────────────────────

def _situation(root, node_type, node_inputs):
    return {
        "upstream_fingerprint": dispatch_gate.upstream_fingerprint(root, node_type),
        "inputs_fingerprint": dispatch_gate.inputs_fingerprint(node_inputs),
    }


def test_refuses_when_nothing_changed(tmp_path):
    root = _worktree(tmp_path)
    _bound(tmp_path, "experiment", root).save_artifact("clean_results", "r", "v1", {})
    prior = _situation(root, "writing", {"task_id": "T04"})

    decision = dispatch_gate.evaluate(
        blockers=[BLOCKER], prior_situation=prior, worktree=root,
        node_type="writing", node_inputs={"task_id": "T04"})
    assert decision is not None
    assert decision["kind"] == "blocked_situation_unchanged"


def test_allows_after_upstream_actually_changed(tmp_path):
    root = _worktree(tmp_path)
    _bound(tmp_path, "experiment", root).save_artifact("clean_results", "r", "v1", {})
    prior = _situation(root, "writing", {"task_id": "T04"})
    _bound(tmp_path, "experiment", root, run_id="e2").save_artifact(
        "experiment_log", "r2", "补齐了", {})

    assert dispatch_gate.evaluate(
        blockers=[BLOCKER], prior_situation=prior, worktree=root,
        node_type="writing", node_inputs={"task_id": "T04"}) is None


def test_allows_when_the_caller_changed_its_request(tmp_path):
    """解除路径 (b)：调用方指名版本 / 改要求，是一次真实扰动。"""
    root = _worktree(tmp_path)
    prior = _situation(root, "writing", {"task_id": "T04"})
    assert dispatch_gate.evaluate(
        blockers=[BLOCKER], prior_situation=prior, worktree=root,
        node_type="writing",
        node_inputs={"task_id": "T04", "mode": "gap_report"}) is None


def test_key_order_in_node_inputs_is_not_a_change(tmp_path):
    root = _worktree(tmp_path)
    prior = _situation(root, "writing", {"a": 1, "b": 2})
    assert dispatch_gate.evaluate(
        blockers=[BLOCKER], prior_situation=prior, worktree=root,
        node_type="writing", node_inputs={"b": 2, "a": 1}) is not None


def test_old_records_without_a_situation_snapshot_are_let_through(tmp_path):
    """证据不在场时放行 —— 不把"没扫过"当成"局面没变"。"""
    root = _worktree(tmp_path)
    assert dispatch_gate.evaluate(
        blockers=[BLOCKER], prior_situation=None, worktree=root,
        node_type="writing", node_inputs={}) is None
    assert dispatch_gate.evaluate(
        blockers=[BLOCKER], prior_situation={}, worktree=root,
        node_type="writing", node_inputs={}) is None


def test_no_blocker_no_gate(tmp_path):
    root = _worktree(tmp_path)
    assert dispatch_gate.evaluate(
        blockers=[], prior_situation=_situation(root, "writing", {}),
        worktree=root, node_type="writing", node_inputs={}) is None


def test_env_switch_turns_the_gate_off(tmp_path, monkeypatch):
    root = _worktree(tmp_path)
    prior = _situation(root, "writing", {})
    monkeypatch.setenv("HARNESS_BLOCKER_DISPATCH_GATE", "off")
    assert dispatch_gate.evaluate(
        blockers=[BLOCKER], prior_situation=prior, worktree=root,
        node_type="writing", node_inputs={}) is None


# ── 3. 报错必须写全解除路径 ─────────────────────────────────────────────────

def test_refusal_names_the_blocker_and_all_three_escapes():
    text = dispatch_gate.render_refusal(
        {"node_type": "writing", "blockers": [BLOCKER]}, ["experiment", "data"])
    assert BLOCKER["summary"] in text
    assert BLOCKER["requested_action"] in text
    assert "experiment" in text                     # (a) 上游候选
    assert "node_inputs" in text                    # (b) 改要求
    assert "CONTINUOUS_STATUS: blocked" in text     # (c) 交给人


def test_refusal_quotes_the_reporter_when_retry_cannot_help():
    text = dispatch_gate.render_refusal(
        {"node_type": "writing",
         "blockers": [{**BLOCKER, "retryable_after_change": False}]}, [])
    assert "光是重跑没有用" in text


# ── 4. 账本层：blocked run 在既有熔断器眼里是成功的 ─────────────────────────

def test_a_blocked_run_reads_as_success_to_the_failure_breaker(tmp_path):
    """这条是本 issue 的病根，钉住它免得有人"顺手"改回去。

    报了 blocker 的 run 必需产出齐 → `reevaluated_success` 判它成了 →
    `consecutive_failures` 断链。所以重复失败熔断**永远**拦不住这条路径，
    必须另有一道闸。
    """
    base = tmp_path / "runs"
    base.mkdir()
    for name in ("1000-a", "1001-b"):
        d = base / name
        d.mkdir()
        (d / "summary.json").write_text(json.dumps({
            "node_type": "writing", "project_id": "p", "status": "blocked",
            "missing_required_outputs": [],
            "quality_check_results": [{"name": "x", "passed": True}],
            "artifacts": [{"id": "manuscript__gap", "type": "manuscript"}],
            "blockers": [BLOCKER],
            "blocked_situation": {"upstream_fingerprint": "abc",
                                  "inputs_fingerprint": "def"},
        }), encoding="utf-8")

    runs = run_history.load_runs(base, project_id="p")
    # blocked ≠ 成功（不再平反），也 ≠ 失败（零信号 → 熔断器跳过不计数）
    assert runs and all(not r.is_completed for r in runs)
    assert runs and all(not r.failure_signals for r in runs)
    assert run_history.consecutive_failures(runs, "writing") is None
    # 而新的闸看得见它：结构化 blocker + 局面快照都在 RunRecord 上
    assert runs[0].blockers and runs[0].blockers[0]["category"] == "missing_input"
    assert runs[0].blocked_situation == {"upstream_fingerprint": "abc",
                                         "inputs_fingerprint": "def"}


# ── 5. 接线：判据存在 ≠ 判据在路径上 ────────────────────────────────────────

def _blocked_sibling(runs_dir, root, *, node_type="writing", name="1000-w",
                     project_id="p", node_inputs=None):
    d = runs_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "node_type": node_type, "project_id": project_id, "status": "blocked",
        "missing_required_outputs": [],
        "quality_check_results": [{"name": "x", "passed": True}],
        "artifacts": [], "blockers": [BLOCKER],
        "blocked_situation": {
            "upstream_fingerprint": dispatch_gate.upstream_fingerprint(root, node_type),
            "inputs_fingerprint": dispatch_gate.inputs_fingerprint(node_inputs or {}),
        },
    }), encoding="utf-8")
    return d


def _orchestrator(tmp_path, root, runs_dir, project_id="p"):
    (runs_dir / "orch").mkdir(parents=True, exist_ok=True)
    state = State(run_id="orch", node_type="_orchestrator",
                  root=runs_dir / "orch", project_id=project_id)
    bind_project_workspace(state, root)
    return state


def test_run_node_refuses_the_repeat_dispatch(tmp_path):
    """真正要验的是这一条：闸接在派发那一刻的调用链上，不是只存在于模块里。"""
    from shared.tools.run_node import _blocked_dispatch_refusal

    root = _worktree(tmp_path)
    _bound(tmp_path, "experiment", root).save_artifact("clean_results", "r", "v1", {})
    runs_dir = tmp_path / "runs"
    _blocked_sibling(runs_dir, root, node_inputs={"task_id": "T04"})
    state = _orchestrator(tmp_path, root, runs_dir)

    refusal = _blocked_dispatch_refusal(state, "writing", {"task_id": "T04"})
    assert refusal is not None
    assert refusal["status"] == "error"
    assert refusal["blocker"]["kind"] == "blocked_situation_unchanged"
    assert BLOCKER["requested_action"] in refusal["error"]


def test_run_node_lets_it_through_once_upstream_moved(tmp_path):
    from shared.tools.run_node import _blocked_dispatch_refusal

    root = _worktree(tmp_path)
    _bound(tmp_path, "experiment", root).save_artifact("clean_results", "r", "v1", {})
    runs_dir = tmp_path / "runs"
    _blocked_sibling(runs_dir, root, node_inputs={"task_id": "T04"})
    _bound(tmp_path, "experiment", root, run_id="e2").save_artifact(
        "experiment_log", "r2", "补齐了", {})
    state = _orchestrator(tmp_path, root, runs_dir)

    assert _blocked_dispatch_refusal(state, "writing", {"task_id": "T04"}) is None


def test_run_node_gate_ignores_system_nodes(tmp_path):
    from shared.tools.run_node import _blocked_dispatch_refusal

    root = _worktree(tmp_path)
    runs_dir = tmp_path / "runs"
    _blocked_sibling(runs_dir, root, node_type="_reviewer", name="1000-rv")
    state = _orchestrator(tmp_path, root, runs_dir)
    assert _blocked_dispatch_refusal(state, "_reviewer", {}) is None


def test_a_later_clean_run_clears_the_gate(tmp_path):
    """闸只看**最近一次** —— 节点后来跑成过，旧的阻塞不该继续锁着它。"""
    from shared.tools.run_node import _blocked_dispatch_refusal

    root = _worktree(tmp_path)
    runs_dir = tmp_path / "runs"
    _blocked_sibling(runs_dir, root, name="1000-w")
    later = runs_dir / "2000-w"
    later.mkdir(parents=True)
    (later / "summary.json").write_text(json.dumps({
        "node_type": "writing", "project_id": "p", "status": "completed",
        "missing_required_outputs": [],
        "artifacts": [], "blockers": [],
    }), encoding="utf-8")
    state = _orchestrator(tmp_path, root, runs_dir)
    assert _blocked_dispatch_refusal(state, "writing", {}) is None


def test_executor_actually_captures_the_situation_on_a_blocked_run():
    """跨层接线机械核对：finalize_run 里必须真的引用 capture 与那个 key。

    "我加了个字段"和"那个字段真的会被写出来"是两件事（PR#410 的教训）。
    """
    from core.executor import finalize_run

    names = set(finalize_run.__code__.co_names)
    assert "capture" in names
    assert "BLOCKED_SITUATION_KEY" in names


def test_capture_shape(tmp_path):
    root = _worktree(tmp_path)
    state = _bound(tmp_path, "writing", root)
    captured = dispatch_gate.capture(state, "writing", {"task_id": "T04"})
    assert set(captured) == {"upstream_fingerprint", "inputs_fingerprint"}
    assert captured["upstream_fingerprint"] == dispatch_gate.upstream_fingerprint(
        root, "writing")


def test_capture_without_a_worktree_records_nothing(tmp_path):
    state = State(run_id="x", node_type="writing", root=tmp_path / "r")
    assert dispatch_gate.capture(state, "writing", {}) is None
