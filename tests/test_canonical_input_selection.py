"""调用方没指名时，别让子节点自己猜用哪一份上游产物（issue #522）。

现场：hypothesis 与 experiment 各跑了多轮，留下多份 pre_registration /
experiment_log。writing 被派起来时 `forward_artifact_ids` 是空的，v2 的
"子节点直读共享 worktree"于是把**选料**整个留空 —— 框架一句话都不说，writing
面对多份材料无法确定哪一组属于同一条研究链路，连出两份"材料不足报告"
（run 1787098098-542bb9 / 1787099478-4a8a3c）。

判据必须机械：框架只数"这个类型上有几个候选"，不猜谁取代谁。
"""
from __future__ import annotations

import os
import subprocess

import pytest

from core.bootstrap import bootstrap
from core.project_workspace import bind_project_workspace
from core.state import State
from shared.tools.run_node import _canonical_input_selection

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
    rid = run_id or f"r-{node_type}"
    state = State(run_id=rid, node_type=node_type, root=tmp_path / f"run-{rid}")
    (tmp_path / f"run-{rid}").mkdir(parents=True, exist_ok=True)
    bind_project_workspace(state, root)
    return state


def test_single_candidate_is_resolved_for_the_caller(tmp_path):
    """只有一份的时候没有第二种可能 —— 框架替调用方选定，别让它去猜。"""
    root = _worktree(tmp_path)
    _bound(tmp_path, "experiment", root).save_artifact(
        "experiment_log", "run1", "log", {})
    caller = _bound(tmp_path, "_orchestrator", root)

    ids, refusal = _canonical_input_selection(caller, "writing", ["experiment_log"])
    assert refusal is None
    assert ids == ["experiment_log__run1"]


def test_two_candidates_refuse_and_list_them(tmp_path):
    root = _worktree(tmp_path)
    experiment = _bound(tmp_path, "experiment", root)
    experiment.save_artifact("experiment_log", "round1", "log v1", {})
    experiment.save_artifact("experiment_log", "round2", "log v2", {})
    caller = _bound(tmp_path, "_orchestrator", root)

    ids, refusal = _canonical_input_selection(caller, "writing", ["experiment_log"])
    assert ids == []
    assert refusal is not None
    assert refusal["blocker"]["kind"] == "input_version_conflict"
    listed = refusal["version_conflict"]["experiment_log"]
    assert set(listed) == {"experiment_log__round1", "experiment_log__round2"}
    # 报错必须让**调用方**能直接动手：候选 + 怎么指名
    assert "forward_artifact_ids" in refusal["error"]
    for artifact_id in listed:
        assert artifact_id in refusal["error"]


def test_new_version_of_one_identity_is_not_a_conflict(tmp_path):
    """同一身份的第 2 版不是歧义 —— head 就是当前版本（PR #500 三原语）。"""
    root = _worktree(tmp_path)
    experiment = _bound(tmp_path, "experiment", root)
    experiment.save_artifact("experiment_log", "round1", "log v1", {})
    experiment.save_artifact("experiment_log", "round1", "log v2 修订", {})
    caller = _bound(tmp_path, "_orchestrator", root)

    ids, refusal = _canonical_input_selection(caller, "writing", ["experiment_log"])
    assert refusal is None
    assert ids == ["experiment_log__round1"]


def test_no_candidate_is_not_a_conflict(tmp_path):
    """材料还没有是合法局面 —— 节点该如实产降级产物，不是被框架拦在门外。"""
    root = _worktree(tmp_path)
    caller = _bound(tmp_path, "_orchestrator", root)
    ids, refusal = _canonical_input_selection(caller, "writing", ["experiment_log"])
    assert (ids, refusal) == ([], None)


def test_node_without_declared_inputs_is_untouched(tmp_path):
    root = _worktree(tmp_path)
    experiment = _bound(tmp_path, "experiment", root)
    experiment.save_artifact("experiment_log", "a", "x", {})
    experiment.save_artifact("experiment_log", "b", "y", {})
    caller = _bound(tmp_path, "_orchestrator", root)
    assert _canonical_input_selection(caller, "literature", []) == ([], None)


def test_the_gate_is_actually_wired_into_dispatch():
    """判据存在 ≠ 判据在路径上（#522 的病根本身就是"选料"没接到 v2 路径）。"""
    import inspect

    from shared.tools import run_node

    source = inspect.getsource(run_node._run_node_tool)
    assert "_canonical_input_selection" in source
