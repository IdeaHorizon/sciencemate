"""子节点的 checkpoint 不能被父节点的**下一次**工具调用当成越界回退。

## 现场（2026-08-11 实测，真实数据丢失）

    10:20  orchestrator 派发 experiment → 平台 checkpoint 0d275be（157 文件）
    10:34  orchestrator 再派发一个节点（run_node）
           → project_write_boundary_blocked
             tool=run_node  node=_orchestrator  owned=notes
             reverted 164 path(s)，第一条是 .git/HEAD
    结果   6 份 LAMMPS 生产日志从磁盘消失，commit 14 → 13

## 2026-08-08 修过一次，只覆盖了一半

`enforce_after_tool` 的注释记着 E2E v10 那次：

    这道门原本只问"HEAD 动没动"，动了就一律回退…于是合法 checkpoint 被当成
    越界…竞态，所以时灵时不灵。判据不该是"HEAD 动没动"，而该是这些提交碰的
    路径在不在允许范围内。

那次让**派发它的那一次调用**放行（子节点目录进 `permitted`）。但：

  - `note_delegated_workspace` 的生命周期 = **这一次工具调用**（用完即清）
  - 平台 checkpoint 之后，**没有任何东西**更新
    `_project_workspace_expected_head`（全仓只有两处写：绑定时、守卫自己的
    合法推进分支）

于是**下一次**工具调用拿着陈旧基线、带着新的 permitted 集合，把上一次的
合法 checkpoint 判成越界。

这条测试跑两次连续的工具调用 —— 第一次派发 A，第二次派发 B —— 断言 A 的
commit 在第二次之后**还在**。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.project_workspace import (
    _DELEGATED_KEY,
    enforce_after_tool,
    note_delegated_workspace,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class _State:
    """只带守卫需要的字段。node_type = _orchestrator。"""

    def __init__(self, worktree: Path) -> None:
        self.project_worktree = worktree
        self.workspace_relative_path = "notes"
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
    for node in ("experiments", "plan", "notes"):
        (root / node).mkdir(parents=True, exist_ok=True)
        (root / node / ".keep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "session: initialize")
    return root


def _platform_checkpoint(root: Path, node: str, filename: str) -> str:
    """模拟平台给刚跑完的子节点提交它的交付物（只碰子节点自己的目录）。"""
    from core.project_workspace import _NODE_WORKSPACES

    (root / _NODE_WORKSPACES[node] / filename).write_text("result\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"node({node}): checkpoint run-1")
    return _git(root, "rev-parse", "HEAD")


def test_a_child_checkpoint_survives_the_next_dispatch(worktree: Path) -> None:
    """两次连续的 run_node —— 第一次派发 experiment，第二次派发 hypothesis。

    这正是 2026-08-11 丢数据的形状：experiment 的 checkpoint 在第二次派发时
    被判越界、连 `.git/HEAD` 一起回退。
    """
    state = _State(worktree)

    # ── 工具调用 1：派发 experiment，平台给它 checkpoint ──────────────────
    note_delegated_workspace(state, "experiment")
    checkpoint = _platform_checkpoint(worktree, "experiment", "prod_T0.60_r1.log")
    enforce_after_tool(state, "run_node")

    assert _git(worktree, "rev-parse", "HEAD") == checkpoint, (
        "派发它的那一次调用就不该回退（2026-08-08 已修）"
    )

    # ── 工具调用 2：派发另一个节点，没有新提交 ────────────────────────────
    note_delegated_workspace(state, "hypothesis")
    report = enforce_after_tool(state, "run_node")

    assert _git(worktree, "rev-parse", "HEAD") == checkpoint, (
        f"下一次工具调用把上一次的 checkpoint 回退了：{report}"
    )
    assert (worktree / "experiments/prod_T0.60_r1.log").exists(), (
        "实验数据被守卫删了 —— 这就是 10:34 那次 164 条回退"
    )


def test_a_checkpoint_that_landed_outside_a_tool_call_still_survives(worktree: Path) -> None:
    """checkpoint 落在**恢复暂停**的路径上 —— 那条路不经过工具调用守卫。

    真实形状（2026-08-11）：orchestrator 09:04 派发 experiment，中间因高危
    审批**暂停 5 次**，每次由人回答后从 `pause_driver` 恢复；平台在 10:20
    子节点收尾时 checkpoint。恢复路径不是"工具调用"，所以
    `enforce_after_tool` 不跑，基线 `_project_workspace_expected_head`
    停在 09:03 的会话起点。

    到 10:34 下一次真正的工具调用时，守卫拿着**陈旧基线**和**新的 permitted
    集合**，把那次合法 checkpoint 判成越界 —— 回退 164 条，含 `.git/HEAD`。

    所以这条用例**故意不调** call 1 的 enforce_after_tool。
    """
    state = _State(worktree)

    # 工具调用 1：派发 experiment；checkpoint 落在之后的恢复路径上，
    # 守卫没有机会更新基线。
    note_delegated_workspace(state, "experiment")
    checkpoint = _platform_checkpoint(worktree, "experiment", "prod_T0.60_r1.log")
    # ← 此处**不调** enforce_after_tool（恢复路径不经过它）
    state.hook_state.pop(_DELEGATED_KEY, None)   # 用真实常量，别写字面量

    # 工具调用 2：派发别的节点
    note_delegated_workspace(state, "hypothesis")
    report = enforce_after_tool(state, "run_node")

    assert _git(worktree, "rev-parse", "HEAD") == checkpoint, (
        f"陈旧基线把上一次合法 checkpoint 回退了：{report}"
    )
    assert (worktree / "experiments/prod_T0.60_r1.log").exists(), (
        "实验数据被守卫删了 —— 这就是 10:34 那次 164 条回退"
    )


def test_a_genuine_boundary_violation_is_reported_not_reverted(worktree: Path) -> None:
    """越界提交要留证据 —— 但**不回退**（断言在 2026-08-11 当天翻过来）。

    这条用例上一版断言的是"必须被冲掉"。同一天下午查清了那个"冲掉"的真实
    代价：`reset --mixed` 把刚提交的文件变回未跟踪，本函数下半段再按"越界的
    未提交改动"把它们**删掉**。两段各自无害，组合起来是硬删除 —— 157 个文件、
    6 份 LAMMPS 生产日志就是这么没的，而那个 commit 完全合法。

    防线因此移到了两个拥有第一手事实的地方：
      - 闸口：改写历史的 git 直接不给（`match_git_authority_violation`）
      - 平台：checkpoint 前拿自己 DB 的 head 比对磁盘，不符就拒绝写

    这一层只留证据。见 tests/test_commit_authority_is_gated_not_forensic.py。
    """
    state = _State(worktree)

    # 没有派发任何子节点，却出现了碰 experiment/ 的提交 → 归因不到
    (worktree / "experiments/sneaky.txt").write_text("x\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "shell did this")
    head = _git(worktree, "rev-parse", "HEAD")

    report = enforce_after_tool(state, "safe_run_bash")

    # 归因不到不等于把这次工具调用判失败：没有 capture 基线 → 不点名任何路径；
    # 但也不假报「干净」—— 如实说这次没见证（2026-09-02，tool_registry:634 对称面）。
    assert report is not None and report["witness_unavailable"] is True
    assert report["paths"] == [], "归因不到不等于把这次工具调用判失败"
    assert _git(worktree, "rev-parse", "HEAD") == head, "不回退"
    assert (worktree / "experiments/sneaky.txt").exists()
    # 2026-08-13 起分类只看**快进与否**：模型根本做不出提交（git 写命令在
    # 闸口硬拒 + 沙箱可写面不含 .git），所以每个快进提交按构造就是平台做的。
    # "这个提交碰的路径在不在我派发过的子树里"那套 run 级委派记账已删 ——
    # 它曾是误删实验数据的推断链条的一环。证据（含路径清单）照留。
    events = dict((n, f) for n, f in state.transcript)
    assert "workspace_head_advanced" in events
    assert "experiments/sneaky.txt" in events["workspace_head_advanced"]["paths"]


def test_working_tree_writes_keep_the_per_call_scope(worktree: Path) -> None:
    """越界写的**见证**仍按"这一次工具调用"判：上次派发过 experiment，
    不等于这次拿 shell 写 experiment/ 不被报。

    墙在沙箱（写的那一刻拒掉，见 test_out_of_bounds_writes_fail_at_write_time）；
    这里是降级部署下的见证 —— 报告，不动文件（销毁能力 2026-08-13 下线）。
    """
    from core.project_workspace import capture_before_tool

    state = _State(worktree)

    # 工具调用 1：派发 experiment（委派记录用完即清）
    capture_before_tool(state)
    note_delegated_workspace(state, "experiment")
    enforce_after_tool(state, "run_node")

    # 工具调用 2：没有派发任何人，却用 shell 往 experiment/ 写了个文件
    capture_before_tool(state)
    (worktree / "experiments/shell_wrote_this.txt").write_text("x\n", encoding="utf-8")
    report = enforce_after_tool(state, "safe_run_bash")

    assert report is not None and "experiments/shell_wrote_this.txt" in report["paths"], (
        "上一次的委派不该漂移到这一次 —— 见证的作用域仍是单次调用"
    )
    assert (worktree / "experiments/shell_wrote_this.txt").exists(), (
        "见证层不许动文件"
    )
