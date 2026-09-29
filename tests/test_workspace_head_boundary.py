"""写边界的**同一条规则**必须同时管未提交改动和已提交改动（E2E v10）。

现场：orchestrator 在 run_node 里派 literature；literature 跑完，平台的
checkpoint 权威给它提交交付物 → HEAD 前进；父节点的 enforce_after_tool 拿着
派发前拍的基线一比，把这次合法 checkpoint 当越界，`reset --mixed` 冲掉了它。
数据库还记着新 sha → 下一次 checkpoint 报 "Session Git head changed outside
Platform checkpoint authority"，整条 run 死在这。竞态，所以时灵时不灵。

判据不是"HEAD 动没动"，而是"这些提交碰的路径在不在允许范围内" —— 与工作区
改动同一条规则，两种载体。

## 2026-08-11 更新：路径取证判得对，但**不该由它来判**

上面那条判据本身没错，错的是它拿什么当基线：一份记在 harness 内存里的
`_project_workspace_expected_head`。同一个事实平台自己也记着（DB 的
`git_head_commit_sha`），两份抄件分叉时**两边都不报错**。

真出事那次，平台记的是对的（0d275be），harness 那份停在会话起点，于是把一个
完全合法的 `node(experiment): checkpoint` 判成越界 —— 157 个文件、6 份 LAMMPS
生产日志（78 分钟机时）从磁盘消失。而"冲掉"看着无害（`reset --mixed` 只动
ref 和 index），实际上和 enforce 下半段组合成硬删除。

改法是把这条不变量放回拥有第一手事实的两个地方：

    闸口   改写历史的 git 在 dangerous_commands 里硬拒（节点连做的能力都没有）
    平台   checkpoint 前拿自己 DB 的 head 比对磁盘，不符 fail-closed 拒绝写

这一层只留证据（`workspace_head_changed_unattributed`），不再回退。
下面几条断言因此在同一天翻转过。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.project_workspace import (
    _committed_paths_between,
    bind_project_workspace,
    capture_before_tool,
    enforce_after_tool,
    note_delegated_workspace,
)
from core.state import State

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _run(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, env=_ENV).stdout



def _events(state, event_type: str) -> list[dict]:
    """从真 transcript 文件里读事件 —— 证明它落到了平台读得到的地方。"""
    import json
    path = state.transcript_path
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") == event_type:
            out.append(record)
    return out


def _worktree(tmp_path) -> Path:
    root = tmp_path / "wt"
    root.mkdir()
    _run(root, "init", "-q")
    _run(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


def _commit(root: Path, relative: str, body: str, message: str) -> str:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _run(root, "add", "-A")
    _run(root, "commit", "-q", "-m", message)
    return _run(root, "rev-parse", "HEAD").strip()


def _orchestrator(tmp_path, root) -> State:
    run_root = tmp_path / "run-orc"
    run_root.mkdir(parents=True, exist_ok=True)
    state = State(run_id="orc", node_type="_orchestrator", root=run_root)
    bind_project_workspace(state, root)
    return state


def test_platform_checkpoint_of_a_dispatched_child_is_not_a_violation(tmp_path):
    root = _worktree(tmp_path)
    state = _orchestrator(tmp_path, root)
    head_before = _run(root, "rev-parse", "HEAD").strip()

    capture_before_tool(state)
    note_delegated_workspace(state, "literature")           # 派发子节点
    new_head = _commit(root, "literature/artifacts/pkg.json", "{}",
                       "node(literature): checkpoint 1786070189-61d8df")

    report = enforce_after_tool(state, "run_node")
    assert report is None, f"合法 checkpoint 被当成越界：{report}"
    assert _run(root, "rev-parse", "HEAD").strip() == new_head   # 没被冲掉
    # 基线抬到新 HEAD —— 否则下一次工具调用会把同一个 checkpoint 再判一次
    assert state.hook_state["_project_workspace_expected_head"] == new_head
    assert head_before != new_head


def test_commit_outside_the_permitted_scope_is_reported(tmp_path):
    """越界提交要留证据 —— 但不再回退（断言 2026-08-11 翻转）。

    "回退"看着无害（只动 ref 和 index），实际上和本函数下半段组合成硬删除：
    刚提交的文件退回未跟踪 → 下半段按"越界的未提交改动"删掉。真实代价是
    157 个文件、6 份 LAMMPS 生产日志，而那次的 commit 完全合法。

    拦是移到闸口拦的：改写历史的 git 在 dangerous_commands 里硬拒，节点连做
    的能力都没有。见 tests/test_commit_authority_is_gated_not_forensic.py。
    """
    root = _worktree(tmp_path)
    state = _orchestrator(tmp_path, root)

    capture_before_tool(state)
    new_head = _commit(root, "paper/manuscript.md", "偷偷写的", "sneaky")

    report = enforce_after_tool(state, "run_bash")
    assert report is None
    assert _run(root, "rev-parse", "HEAD").strip() == new_head       # 不回退
    assert (root / "paper/manuscript.md").exists()
    # 2026-08-13：分类只看快进与否 —— 模型做不出提交（闸口 + 沙箱），快进
    # 提交按构造是平台的。证据（路径清单）留在 advanced 事件里。
    recorded = _events(state, "workspace_head_advanced")
    assert recorded, "必须留证据"
    assert "paper/manuscript.md" in recorded[0]["paths"]


def test_a_backwards_head_is_the_unattributed_case(tmp_path):
    """快进 = 平台在推进（记 advanced + 路径）；**非快进**才是要吵的形状。"""
    root = _worktree(tmp_path)
    newer = _commit(root, "a.txt", "a", "second")
    state = _orchestrator(tmp_path, root)          # 基线 = second

    capture_before_tool(state)
    _run(root, "reset", "-q", "--hard", "HEAD~1")  # HEAD 往回走

    enforce_after_tool(state, "run_node")
    assert _events(state, "workspace_head_changed_unattributed"), (
        "回退 / 换分支说不清 —— 必须吵一声，不能静默"
    )


def test_own_scope_commit_is_allowed(tmp_path):
    root = _worktree(tmp_path)
    state = _orchestrator(tmp_path, root)
    capture_before_tool(state)
    new_head = _commit(root, ".research/orchestration/notes.md", "x", "own scope")
    assert enforce_after_tool(state, "run_node") is None
    assert _run(root, "rev-parse", "HEAD").strip() == new_head


def test_non_fast_forward_is_fail_closed(tmp_path):
    """HEAD 回退 / 换分支 —— 说不清就当越界。fail-closed 写在返回类型里。"""
    root = _worktree(tmp_path)
    base = _run(root, "rev-parse", "HEAD").strip()
    other = _commit(root, "a.txt", "a", "a")
    _run(root, "reset", "-q", "--hard", base)
    # other 不是 base 的后代方向 —— base..other 不是快进关系时返回 None
    assert _committed_paths_between(root, other, base) is None


def test_unreadable_refs_are_fail_closed(tmp_path):
    root = _worktree(tmp_path)
    assert _committed_paths_between(root, "deadbeef" * 5, "HEAD") is None
    assert _committed_paths_between(root, "", "HEAD") is None
