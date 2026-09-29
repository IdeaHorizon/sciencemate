"""事后见证只留证据 —— 不回退、不隔离、不删除。

## 为什么守卫没有销毁能力了（2026-08-13）

事后层判的是"文件系统上出现了什么"，不是"谁写的"。作者只能推断，而推断
错过两回，代价都是真数据：

    2026-08-11  陈旧基线 + reset --mixed 组合成硬删除：157 个文件、
                6 份 LAMMPS 生产日志、一份从未提交的评审意见（永久丢失）
    2026-08-13  orchestrator 写进**自己目录**的三份产物被隔离 ——
                curator 的守卫看到"不属于我的新文件"，它没法知道那是
                父节点的正当写入

墙移到了 spawn 那一刻（core/sandbox.py 进程沙箱）；提交权威在闸口和平台
（test_commit_authority_is_gated_not_forensic.py）。这一层退役成见证：
报告 + 指路，零动作。误报的代价从"不可再生的数据没了"降到"多一行日志"。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.project_workspace import capture_before_tool, enforce_after_tool


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class _State:
    def __init__(self, worktree: Path) -> None:
        self.project_worktree = worktree
        self.workspace_relative_path = ".research/orchestration"
        self.node_type = "_orchestrator"
        self.hook_state: dict = {
            "_project_workspace_expected_head": _git(worktree, "rev-parse", "HEAD"),
        }
        self.transcript: list = []

    def append_transcript(self, event: str, **fields) -> None:
        self.transcript.append((event, fields))


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for node in ("experiments", "reviews", "notes"):
        (root / node).mkdir(parents=True, exist_ok=True)
        (root / node / ".keep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "session: initialize")
    return root


def test_an_escape_is_reported_with_owner_and_next_step(worktree: Path) -> None:
    """越界写 → 文件原地不动 + 证据事件 + 指名 owner 和正确做法。"""
    state = _State(worktree)
    capture_before_tool(state)
    escaped = worktree / "experiments/escaped.txt"
    escaped.write_text("x\n", encoding="utf-8")

    report = enforce_after_tool(state, "safe_run_bash")

    assert escaped.exists(), "见证层不许动文件 —— 销毁能力已下线"
    assert escaped.read_text(encoding="utf-8") == "x\n"
    assert report is not None
    assert "experiments/escaped.txt" in report["paths"]
    assert "experiment" in report["note"] and "run_node" in report["note"], (
        "报告必须指名 owner 和正确做法，只说'越界了'等于逼模型猜"
    )
    events = dict((n, f) for n, f in state.transcript)
    assert "workspace_out_of_scope_writes" in events
    assert events["workspace_out_of_scope_writes"]["owners"].get(
        "experiments/escaped.txt") == "experiment"


def test_a_reviewer_critique_survives(worktree: Path) -> None:
    """2026-08-11 的那份评审意见：从未提交过 → 一删就是永久丢失。回放它。"""
    state = _State(worktree)
    capture_before_tool(state)
    critique = worktree / "reviews/artifacts/review_critique__x.json"
    critique.parent.mkdir(parents=True, exist_ok=True)
    critique.write_text('{"verdict": "needs_revision"}', encoding="utf-8")

    enforce_after_tool(state, "safe_run_bash")

    assert critique.exists()
    assert critique.read_text(encoding="utf-8") == '{"verdict": "needs_revision"}'


def test_pre_existing_dirt_is_not_re_reported(worktree: Path) -> None:
    """只报**这一次调用**新出现的。已在场的要么报过、要么是别人的正当在制品
    —— 2026-08-13 被隔离的三份 orchestrator 产物就是后者。"""
    state = _State(worktree)
    leftover = worktree / "experiments/leftover_from_child.txt"
    leftover.write_text("child work\n", encoding="utf-8")

    capture_before_tool(state)
    report = enforce_after_tool(state, "kb_overview")

    assert report is None, "调用前就在场的文件不是这次调用的越界证据"
    assert leftover.exists()


def test_modifying_a_preexisting_outside_file_is_also_witnessed(worktree: Path) -> None:
    """改**已在场**文件的内容也要被见证 —— 篡改上游恰恰是最贵的那类越界。

    只比路径集合会漏掉它（路径没变），所以基线带内容指纹。该见证层只审计
    框架内部写入；模型进程由强制容器边界事前阻止。"""
    state = _State(worktree)
    upstream = worktree / "experiments/results.json"
    upstream.write_text('{"v": 1}', encoding="utf-8")

    capture_before_tool(state)
    upstream.write_text('{"v": "tampered"}', encoding="utf-8")
    report = enforce_after_tool(state, "safe_run_bash")

    assert report is not None and "experiments/results.json" in report["paths"]
    assert upstream.read_text(encoding="utf-8") == '{"v": "tampered"}', (
        "见证不回退 —— 报告了就够，恢复是墙和人（git）的事"
    )


def test_missing_baseline_is_reported_as_unwitnessed_not_clean(worktree: Path) -> None:
    """capture 没跑成 → 归因不了 → 不误报（没有 paths，误报曾经等于误删），
    但也**不假报干净**：None 在调用方读作「本次无越界」，那是假值。

    判决拆除（tool_registry:634 的对称面，2026-09-02）：从前这里返 None。
    """
    state = _State(worktree)
    (worktree / "experiments/appeared.txt").write_text("x\n", encoding="utf-8")

    report = enforce_after_tool(state, "safe_run_bash")     # 没有 capture

    assert report is not None and report["witness_unavailable"] is True
    assert report["paths"] == [], "归因不了就不能点名任何路径"
    assert "没有越界见证" in report["note"]
    assert (worktree / "experiments/appeared.txt").exists()


def test_legacy_quarantine_dirs_stay_out_of_the_witness(worktree: Path) -> None:
    """存量隔离区（守卫还会销毁的年代留下的）不进见证范围 ——
    否则每个老 worktree 每次调用都报一遍旧账。"""
    state = _State(worktree)
    legacy = worktree / ".research/quarantine/20260813T051434/old.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}", encoding="utf-8")

    capture_before_tool(state)
    # 就算它是"这次调用后新出现"的形状也不报：写进隔离区的只有旧版守卫自己
    report = enforce_after_tool(state, "safe_run_bash")

    assert report is None


def test_own_scope_writes_are_never_reported(worktree: Path) -> None:
    state = _State(worktree)
    capture_before_tool(state)
    mine = worktree / ".research/orchestration/artifacts/note.json"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("{}", encoding="utf-8")

    report = enforce_after_tool(state, "save_artifact")

    assert report is None
