"""GIT_UNWRITABLE 挂在结构事实上，不挂在"我们相信调用方会传节点目录"上（WP-01 步骤 3）。

Landlock 是纯放行清单，没有 deny 层：可写根里含了 gitdir 或 worktree 指针文件，就没有任何
规则能再把它拒回去。所以这条不变量在 Landlock 上只能靠**不接**：可写根含 git 路径 → 拒绝派发
并说清该传节点目录。判据落在 ``_native.git_paths_inside_writable`` 这个纯函数上，任何 OS 都能
测；``LinuxBackend.prepare`` 的拒绝只在 Linux 上验。

2026-09-08 在 node20 核过的结构事实：会话工作区是 ``git worktree add`` 出来的，``.git`` 是
指针文件、真 gitdir 在 ``project-repositories/<proj>/.git/worktrees/<sid>``（另一棵树）；而
``core.sandbox.write_roots_for`` 给的可写根是节点目录 + run 根，从不含工作树根。所以平台上
这条判据天然成立 —— 但它必须**每次派发都算**，哪天 manifest 的 rw 挂载把工作树根传进来，
账要自己变红，而不是继续说"守到了"。

## 判据 C（变异）

把 ``linux.py::prepare`` 里 ``if hits:`` 反成 ``if not hits:`` → 本文件的 prepare 两条互换着红。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.isolation import CommandSpec, Invariant, IsolationContractError
from core.isolation._native import enclosing_git_paths, git_paths_inside_writable


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "README").write_text("x\n")
    (root / "experiments").mkdir()
    (root / "experiments" / "README").write_text("node dir\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture()
def linked_worktree(repo: Path, tmp_path: Path) -> Path:
    """平台会话工作区的形状：``.git`` 是指针文件，真 gitdir 在主仓库那棵树里。"""
    wt = tmp_path / "worktrees" / "s1"
    wt.parent.mkdir()
    _git(repo, "worktree", "add", "-q", "-b", "session/s1", str(wt), "main")
    assert (wt / ".git").is_file(), "linked worktree 的 .git 应当是指针文件"
    return wt


# ── 纯函数：任何 OS ──────────────────────────────────────────────────────────


def test_a_node_directory_inside_the_worktree_is_clean(repo: Path) -> None:
    assert git_paths_inside_writable([repo / "experiments"]) == ()


def test_the_worktree_root_as_a_writable_root_is_caught(repo: Path) -> None:
    hits = git_paths_inside_writable([repo])
    assert hits, "工作树根当可写根：.git 目录就在里面，必须亮"
    assert any(git_path == (repo / ".git").resolve() for _root, git_path in hits)


def test_a_linked_worktree_pointer_file_is_caught(linked_worktree: Path) -> None:
    hits = git_paths_inside_writable([linked_worktree])
    assert hits, "指针文件在可写根里就能被改写成指向别处 —— 必须亮"
    assert any(git_path == (linked_worktree / ".git").resolve() for _root, git_path in hits)


def test_a_node_directory_inside_a_linked_worktree_is_clean(linked_worktree: Path) -> None:
    node = linked_worktree / "experiments"
    assert node.is_dir()
    assert git_paths_inside_writable([node]) == (), (
        "节点目录不含指针，真 gitdir 在另一棵树 —— 这正是平台上天然成立的形状"
    )


def test_enclosing_git_paths_names_gitdir_common_dir_and_pointer(linked_worktree: Path) -> None:
    found = enclosing_git_paths(linked_worktree / "experiments")
    names = {p for p in found}
    assert (linked_worktree / ".git").resolve() in names
    assert any("worktrees" in str(p) for p in names), "linked worktree 的真 gitdir 要在列"
    assert any(p.name == ".git" and p.is_dir() for p in names), "common dir 要在列"


def test_a_directory_outside_any_repository_is_clean(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert enclosing_git_paths(plain) == ()
    assert git_paths_inside_writable([plain]) == ()


def test_two_roots_where_one_contains_the_other_s_gitdir(repo: Path, linked_worktree: Path) -> None:
    """可写根 A 是节点目录、可写根 B 恰好是主仓库根：B 含 A 所在仓库的 gitdir，也要亮。"""
    hits = git_paths_inside_writable([linked_worktree / "experiments", repo])
    assert hits


# ── Linux：Landlock 分支真的拒绝派发 ─────────────────────────────────────────

_linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Landlock 是 Linux 的")


def _landlock_only_backend(monkeypatch):
    from core.isolation.linux import LinuxBackend

    backend = LinuxBackend()
    backend._probe()
    if not backend._landlock:
        pytest.skip(f"这台机器 Landlock 写墙不可用：{backend.unavailable_reason}")
    monkeypatch.setattr(backend, "_bwrap", None)   # node20 的样子：没有 bwrap
    return backend


@_linux
def test_landlock_backend_declares_git_unwritable_without_bwrap(monkeypatch) -> None:
    backend = _landlock_only_backend(monkeypatch)
    assert Invariant.GIT_UNWRITABLE in backend.capabilities()


@_linux
def test_landlock_backend_refuses_a_spec_whose_writable_root_holds_git(repo, monkeypatch) -> None:
    backend = _landlock_only_backend(monkeypatch)
    spec = CommandSpec(argv=("/bin/true",), cwd=str(repo), writable_roots=(repo,))
    with pytest.raises(IsolationContractError) as raised:
        backend.prepare(spec, state=None)
    message = str(raised.value)
    assert "GIT_UNWRITABLE" in message
    assert str(repo.resolve()) in message, "报错要指名是哪个可写根"
    assert "node's own directory" in message, "报错要说该传什么"


@_linux
def test_landlock_backend_accepts_the_node_directory(repo, monkeypatch) -> None:
    backend = _landlock_only_backend(monkeypatch)
    node = repo / "experiments"
    spec = CommandSpec(argv=("/bin/true",), cwd=str(node), writable_roots=(node,),
                       readonly_roots=(repo,))
    launch = backend.prepare(spec, state=None)
    try:
        assert launch.argv and "--network" in launch.argv
        assert launch.argv[launch.argv.index("--network") + 1] == "deny"
    finally:
        launch.cleanup()


@_linux
def test_landlock_backend_refuses_the_linked_worktree_root(linked_worktree, monkeypatch) -> None:
    backend = _landlock_only_backend(monkeypatch)
    spec = CommandSpec(argv=("/bin/true",), cwd=str(linked_worktree),
                       writable_roots=(linked_worktree,))
    with pytest.raises(IsolationContractError, match="GIT_UNWRITABLE"):
        backend.prepare(spec, state=None)
