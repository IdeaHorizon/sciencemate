"""P3c：科学裁决权归 Analysis（research_state），不归做实验的人。

做实验的人同时当裁判 —— 这是 infeasible 循环证伪、合取项没测全也敢 refuted
等一系列事故的共同根因。这里钉死那道权威边界。

夹具照现行记录模型：research_state 是 `plan/` 下的原生文件，版本与出处在工作区
账本（`core/ledger`）里；同一身份连续 save 就是连续版本，head 是最新版。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.ledger import write_record
from core.project_workspace import _NODE_WORKSPACES as _DIRS
from core.verdict_authority import analysis_verdict_for, scientific_verdict_block


class _S:
    def __init__(self, worktree, node_type="experiment"):
        self.project_worktree = worktree
        self.node_type = node_type


def _worktree(tmp_path) -> Path:
    root = tmp_path / "wt"
    (root / _DIRS["hypothesis"]).mkdir(parents=True)
    (root / _DIRS["experiment"]).mkdir(parents=True)
    return root


def _write_state(root: Path, version: int, rows: list[dict]) -> None:
    """按版本原语落一版 research_state：同一身份再 save 一次就是下一版。"""
    write_record(
        root, artifact_type="research_state", name="research_state",
        content=f"# Research State v{version}", directory=_DIRS["hypothesis"],
        metadata={"version": version, "verdict": "continue", "hypotheses": rows},
        produced_by_node_type="hypothesis", produced_by_run_id="r-hyp",
    )


def test_experiment_cannot_flip_without_analysis_backing(tmp_path):
    root = _worktree(tmp_path)
    _write_state(root, 1, [{"id": "H1", "status": "active"}])
    block = scientific_verdict_block(
        _S(root), hypothesis_id="H1", new_status="refuted")
    assert block and "research_state" in block and "H1" in block


def test_experiment_may_flip_when_analysis_already_ruled(tmp_path):
    root = _worktree(tmp_path)
    _write_state(root, 2, [{"id": "H1", "status": "refuted", "evidence": ["exp_1"]}])
    assert scientific_verdict_block(
        _S(root), hypothesis_id="H1", new_status="refuted") is None


def test_direction_must_match(tmp_path):
    """Analysis 判 supported，experiment 不能借机翻成 refuted。"""
    root = _worktree(tmp_path)
    _write_state(root, 1, [{"id": "H1", "status": "supported", "evidence": ["e"]}])
    block = scientific_verdict_block(
        _S(root), hypothesis_id="H1", new_status="refuted")
    assert block and "supported" in block


def test_unknown_hypothesis_blocked(tmp_path):
    root = _worktree(tmp_path)
    _write_state(root, 1, [{"id": "H1", "status": "supported", "evidence": ["e"]}])
    block = scientific_verdict_block(
        _S(root), hypothesis_id="H9", new_status="validated")
    assert block and "H9" in block


def test_missing_hypothesis_id_blocked_once_analysis_exists(tmp_path):
    root = _worktree(tmp_path)
    _write_state(root, 1, [{"id": "H1", "status": "supported", "evidence": ["e"]}])
    assert scientific_verdict_block(
        _S(root), hypothesis_id="", new_status="validated")


def test_latest_version_wins(tmp_path):
    """v10 必须压过 v9 —— 字符串排序会把 v10 排在 v9 前面。"""
    root = _worktree(tmp_path)
    _write_state(root, 9, [{"id": "H1", "status": "active"}])
    _write_state(root, 10, [{"id": "H1", "status": "refuted", "evidence": ["e"]}])
    assert analysis_verdict_for(_S(root), "H1")["status"] == "refuted"
    assert scientific_verdict_block(
        _S(root), hypothesis_id="H1", new_status="refuted") is None


def test_non_verdict_statuses_untouched(tmp_path):
    """inconclusive / provisional 本来就不是"裁决成立"，不需要背书。"""
    root = _worktree(tmp_path)
    _write_state(root, 1, [{"id": "H1", "status": "active"}])
    for status in ("inconclusive", "provisional", "open", None):
        assert scientific_verdict_block(
            _S(root), hypothesis_id="H1", new_status=status) is None


def test_architecture_nodes_and_analysis_itself_exempt(tmp_path):
    root = _worktree(tmp_path)
    _write_state(root, 1, [{"id": "H1", "status": "active"}])
    for node in ("_curator", "_reviewer", "hypothesis", "analysis"):
        assert scientific_verdict_block(
            _S(root, node), hypothesis_id="H1", new_status="refuted") is None


def test_no_research_state_keeps_legacy_behaviour(tmp_path):
    """没有 Analysis 参与过的历史/工程路径行为不变。

    这不是可绕的缝：scientific verdict 路径要求 prereg 已冻结，而 hypothesis
    的收尾闸在冻结后强制产出 research_state。
    """
    root = _worktree(tmp_path)
    assert scientific_verdict_block(
        _S(root), hypothesis_id="H1", new_status="refuted") is None


def test_no_worktree_is_blocked_not_waved_through(tmp_path):
    """没绑 worktree = **查不了**，不是"没什么可查"。

    这条原来断言放行，是照着上面那条对称写的裸断言、没有论证。而上面那条的
    理由（prereg 冻结后 hypothesis 收尾闸必然产出 research_state）说的是
    "worktree 里有没有 research_state"，压根不适用于"连 worktree 都没绑、
    Analysis 目录看都看不到"的情形。

    在这条路径下放行，等于整道裁决权限门对它不存在 —— 做实验的人可以随手给
    自己的实验下科学结论，正是这个模块存在要挡的事。v2.1 下每个 producing
    节点的 run 都绑 worktree，绑不上说明配置坏了，那更该拦。
    """
    reason = scientific_verdict_block(_S(None), hypothesis_id="H1", new_status="refuted")

    assert reason is not None
    assert "worktree" in reason          # 说清楚缺的是什么
    assert "Analysis" in reason          # 说清楚该找谁


def test_the_block_message_names_the_node_and_target_status(tmp_path):
    """报错要让模型知道是谁、要翻成什么被拦了，别只丢一句"不允许"。"""
    reason = scientific_verdict_block(
        _S(None, "postprocess"), hypothesis_id="H7", new_status="validated")

    assert "postprocess" in reason and "H7" in reason and "validated" in reason


def test_exempt_nodes_are_unaffected_by_the_worktree_rule(tmp_path):
    """别把治理/架构路径一起拦了 —— 它们本来就不受这道门约束。"""
    for node in ("_curator", "_reviewer", "_orchestrator", "hypothesis", "analysis"):
        assert scientific_verdict_block(
            _S(None, node), hypothesis_id="H1", new_status="refuted") is None
