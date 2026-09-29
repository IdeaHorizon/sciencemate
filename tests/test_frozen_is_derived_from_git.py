"""「冻结」从 Git 现算，不靠产出方自己写一个布尔。

## 为什么换掉 `metadata.frozen`

现在的冻结是**自证**：hypothesis 的工具往自己的 artifact 里写 `frozen: true`，
QC 读这个布尔。想造假，改个字段就行。

而同一件事 Git 记得更硬。真实历史（E2E v25 工作区）：

    5fc591f  node(experiment): checkpoint   ← 实验产物
    90c08b2  node(hypothesis): checkpoint   ← pre_registration 在这里

`90c08b2` 是 `5fc591f` 的祖先。**要造假得改写历史**，而 commit 权限只在平台
手里（harness 只能*请求* checkpoint）。

## 判据是「没改过」，不是「有个冻结时刻」

"在某时刻被冻结"要么落时间戳（又一份可篡改的自证），要么落登记表
（`.frozen.jsonl`，又一个真相源）。下游真正关心的只有一句话：**我开跑之后，
这份预注册还是不是我当时看到的那份。** Git 直接能答。

## 三态，不是两态

    True  一个字都没改过
    False 改过
    None  说不清（不是快进 / git 出错）—— 调用方必须按"不能确认"处理

把"说不清"混进 True，就是"判据静默失效"；混进 False，就是把正常运行判死。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.project_workspace import unchanged_since


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class _State:
    def __init__(self, worktree: Path, head: str | None) -> None:
        self.project_worktree = worktree
        self.hook_state = {"_project_workspace_expected_head": head} if head else {}


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "wt"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "plan").mkdir()
    (root / "plan/prereg.json").write_text('{"h": 1}\n', encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "node(hypothesis): checkpoint")
    return root, _git(root, "rev-parse", "HEAD")


def test_untouched_since_the_run_started(repo) -> None:
    root, head = repo
    # 下游又提交了别的东西 —— 与预注册无关，预注册仍然没被动过。
    (root / "experiments").mkdir()
    (root / "experiments/log.md").write_text("run\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "node(experiment): checkpoint")

    assert unchanged_since(_State(root, head), "plan/prereg.json") is True


def test_a_later_commit_touching_it_is_detected(repo) -> None:
    """冻结之后又改了 —— 这正是这道判据要抓的。"""
    root, head = repo
    (root / "plan/prereg.json").write_text('{"h": 2}\n', encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "sneaky edit")

    assert unchanged_since(_State(root, head), "plan/prereg.json") is False


def test_an_uncommitted_edit_also_counts_as_changed(repo) -> None:
    """只看提交历史，会把**正在被编辑**的文件判成冻结。"""
    root, head = repo
    (root / "plan/prereg.json").write_text('{"h": 3}\n', encoding="utf-8")

    assert unchanged_since(_State(root, head), "plan/prereg.json") is False


def test_a_non_fast_forward_says_i_cannot_tell(repo) -> None:
    """回退 / 换分支不是"合法推进" —— 说不清就返回 None，不许当 True。"""
    root, head = repo
    (root / "plan/prereg.json").write_text('{"h": 4}\n', encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "second")
    _git(root, "reset", "-q", "--hard", "HEAD~1")
    # `head` 现在不是当前 HEAD 的祖先关系里那种"由它推进而来"的形态吗？
    # 是的 —— 所以换一个真正无关的基点来证伪。
    stray = _git(root, "commit-tree", "-m", "unrelated", f"{head}^{{tree}}")
    assert unchanged_since(_State(root, stray), "plan/prereg.json") is None


def test_missing_inputs_say_i_cannot_tell(tmp_path: Path) -> None:
    """没绑工作区 / 没有起点 commit / 空路径 → None，不是 True。"""
    assert unchanged_since(_State(None, "abc"), "x") is None
    assert unchanged_since(_State(tmp_path, None), "x") is None


def test_it_needs_no_metadata_field_at_all(repo) -> None:
    """整条判据不碰任何 metadata —— 这就是它比自证强的地方。

    产出方写不写 `frozen: true`、字段叫什么名字，都影响不了这个结论。
    """
    root, head = repo
    import inspect

    from core import project_workspace

    source = inspect.getsource(project_workspace.unchanged_since)
    assert "metadata" not in source.replace("metadata 写", "").replace("metadata，", "")
    assert unchanged_since(_State(root, head), "plan/prereg.json") is True
