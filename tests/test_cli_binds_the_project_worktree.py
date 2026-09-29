"""CLI 也要走 Project Git 工作区 —— 否则半个仓库的代码只有平台跑得到。

## 现场

Project Git workspace 是 v2.1 的现行架构。但接通它的只有平台：`chat.py` 的
CLI 入口调 `_make_or_load_orchestrator_state` 时不传 `project_worktree`，
于是全仓 70+ 处 `if state.project_worktree is not None:` 在 CLI 下永远走 else。

后果不是"两种模式都能用"，是同一个功能有两种行为、只有一种被维护。实测：

  - CLI 项目 e2e-genetic-life-v4：87 条记忆
  - 平台项目（51 条 claim、24 个 concept 的真课题）：memory 文件都不存在

因为记忆的持久化整条写在 else 分支里；`if` 那边只把候选报给 orchestration 就
返回，而它指名的 `memory_curator` 节点根本不存在。

修法不是给每个断点补"平台分支"（那是把分裂固化成两套要各自维护的实现），
是让 CLI 也绑 —— 之后那些 else 分支就是可删的死代码。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.project_bootstrap import (
    ProjectBootstrapError,
    ensure_project_worktree,
    project_worktree_path,
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf"))
    yield


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True).stdout


def test_creates_a_real_git_workspace():
    root = ensure_project_worktree("demo")
    assert root is not None
    assert (root / ".git").exists()
    # bind_project_workspace 要求它是 worktree 根，不是子目录
    top = _git(root, "rev-parse", "--show-toplevel").strip()
    assert Path(top).resolve() == root.resolve()


def test_skeleton_matches_the_ownership_table():
    """目录名就是所有权表的键 —— 少一个，那个节点的产物就无处可放。"""
    from core.project_workspace import _NODE_WORKSPACES

    root = ensure_project_worktree("demo")
    owned = {v for v in _NODE_WORKSPACES.values()}
    for directory in owned:
        if directory.endswith(".md"):      # MEMORY.md 是文件不是目录
            assert (root / directory).is_file(), directory
        else:
            assert (root / directory).is_dir(), directory


def test_memory_md_is_created_as_the_single_authority():
    root = ensure_project_worktree("demo")
    text = (root / "MEMORY.md").read_text(encoding="utf-8")
    assert "canonical" in text
    assert "## Findings" in text


def test_skeleton_survives_the_first_commit():
    """git 不跟踪空目录 —— 没有 .gitkeep 的话骨架提交完就没了。"""
    root = ensure_project_worktree("demo")
    tracked = _git(root, "ls-files").splitlines()
    assert any(p.startswith("experiments/") for p in tracked)
    assert "MEMORY.md" in tracked


def test_is_idempotent_and_never_reinitialises():
    """再次调用不能动已有历史 —— re-init 会让节点产物的 provenance 整条断掉。"""
    root = ensure_project_worktree("demo")
    (root / "experiments" / "result.txt").write_text("data", encoding="utf-8")
    _git(root, "add", "-A")
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t",
                    "-c", "user.email=t@x", "commit", "-m", "work"],
                   capture_output=True, check=True)
    head_before = _git(root, "rev-parse", "HEAD").strip()

    again = ensure_project_worktree("demo")

    assert again == root
    assert _git(root, "rev-parse", "HEAD").strip() == head_before
    assert (root / "experiments" / "result.txt").read_text(encoding="utf-8") == "data"


def test_non_empty_non_git_directory_is_refused_not_absorbed():
    """目录里已有来路不明的东西 —— 吵，别把它们卷进一个凭空的初始提交。"""
    path = project_worktree_path("demo")
    assert path is not None
    path.mkdir(parents=True)
    (path / "stray.txt").write_text("谁放的？", encoding="utf-8")

    with pytest.raises(ProjectBootstrapError):
        ensure_project_worktree("demo")


def test_adhoc_run_without_project_gets_nothing():
    """没有 project_id 的 run 本来就没有项目级持久状态，绑它无意义。"""
    assert ensure_project_worktree(None) is None
    assert ensure_project_worktree("") is None


def test_bind_accepts_what_bootstrap_produces():
    """端到端：造出来的东西必须能被 bind_project_workspace 收下。

    这两个函数一个建一个校验，分在两个模块 —— 校验规则收紧时（比如要求某个
    文件存在）这条会红，而不是等到真跑 run 才在绑定处炸。
    """
    from core.project_workspace import bind_project_workspace

    root = ensure_project_worktree("demo")

    class _S:
        node_type = "experiment"
        hook_state: dict = {}
        project_worktree = None

    state = _S()
    bind_project_workspace(state, root)          # 不抛就算过
    assert state.project_worktree == root



# ── 绑上之后，记忆那条链必须真的通 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_note_survives_the_run_and_another_node_can_read_it():
    """事故本体：绑了 worktree 的 run 写下的教训，别的节点必须读得到。

    此前平台分支只把候选塞进 `hook_state`（run 内存）就返回，并说"让
    memory_curator 去改 MEMORY.md" —— 而那个节点不存在。于是平台上跑的每个
    课题，经验一条都存不下来：实测一个真课题沉淀了 51 条 claim、24 个
    concept，候选提了 4 条，memory 文件却根本没被创建。
    """
    from core import memory as M
    from core import paths as _paths
    from core.state import State

    wt = ensure_project_worktree("demo")
    base = _paths.home() / "runs"

    producer = State.new(node_type="literature", base_dir=base,
                         project_id="demo", project_worktree=wt)
    M.ensure_skeleton(producer)
    res = M.append_manual(
        producer, text="search_papers 对人文社科话题系统性不匹配",
        section=M.SECTION_PITFALL, nodes=["literature"], run_id="r1")
    assert res["created"] is True

    # 换一个 run（新进程新 state）—— 必须跨 run 存活
    curator = State.new(node_type="_curator", base_dir=base,
                        project_id="demo", project_worktree=wt)
    entries = M.manual_entries(curator)
    assert len(entries) == 1, "教训没跨过 run 边界 —— 又只进了 hook_state"
    assert "search_papers" in entries[0].text


def test_memory_authority_is_the_git_memory_md():
    """记忆的权威是 Git 根的 MEMORY.md —— 不能再有第二份。"""
    from core import memory as M
    from core import paths as _paths
    from core.state import State

    wt = ensure_project_worktree("demo")
    state = State.new(node_type="_curator", base_dir=_paths.home() / "runs",
                      project_id="demo", project_worktree=wt)
    assert M.memory_path(state) == wt / "MEMORY.md"

    # 没绑 worktree = 没有项目记忆，不退回任何"兼容位置"。
    # 上一代的分叉正是从那个退路开始的。
    anon = State.new(node_type="_curator", base_dir=_paths.home() / "runs",
                     project_id="demo")
    assert M.memory_path(anon) is None


def test_user_law_lands_under_a_worktree():
    """用户立的规矩即时生效 —— 平台分支曾把这条通道整个拒掉（还回报 success）。"""
    from core import memory as M
    from core import paths as _paths
    from core.state import State

    wt = ensure_project_worktree("demo")
    state = State.new(node_type="_orchestrator", base_dir=_paths.home() / "runs",
                      project_id="demo", project_worktree=wt)
    M.ensure_skeleton(state)
    M.append_law(state, text="低温段一律用更长的弛豫时间平衡系统",
                 derived_from=["低温段弛豫时间要长"])
    assert "更长的弛豫时间" in M.read_section(state, M.SECTION_LAW)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── 一个项目，多个入口 ────────────────────────────────────────────────────

def test_two_sessions_get_separate_worktrees_on_the_same_project():
    """UI 和 CLI 是同一个项目的两个入口 —— 各自一个 worktree，不抢 Git 写道。

    一个 worktree 只有一条 mutation lane：两个入口同时改同一棵树就是两个写者
    抢一条道（平台为此专门禁掉了 background child）。每个入口开自己的
    `session/<id>` 分支，"两个入口打开同一个项目"才成立。
    """
    from core.project_bootstrap import open_session_worktree

    ui = open_session_worktree("demo", "ui-abc")
    cli = open_session_worktree("demo", "cli-xyz")

    assert ui != cli
    assert (ui / ".git").exists() and (cli / ".git").exists()
    assert _git(ui, "rev-parse", "--abbrev-ref", "HEAD").strip() == "session/ui-abc"
    assert _git(cli, "rev-parse", "--abbrev-ref", "HEAD").strip() == "session/cli-xyz"


def test_sessions_share_one_project_history():
    """两个 session 是同一个仓的两棵树 —— 共享历史，不是两个项目。"""
    from core.project_bootstrap import ensure_project_worktree, open_session_worktree

    repo = ensure_project_worktree("demo")
    base = _git(repo, "rev-parse", "HEAD").strip()
    ui = open_session_worktree("demo", "ui-abc")
    cli = open_session_worktree("demo", "cli-xyz")

    for tree in (ui, cli):
        assert _git(tree, "merge-base", "HEAD", base).strip() == base


def test_opening_the_same_session_twice_is_idempotent():
    """UI 开着的 session，CLI 用同一个 id 打开就是接着它干，不是新建。"""
    from core.project_bootstrap import open_session_worktree

    first = open_session_worktree("demo", "same-id")
    (first / "experiments" / "note.txt").write_text("from UI", encoding="utf-8")
    second = open_session_worktree("demo", "same-id")

    assert second == first
    assert (second / "experiments" / "note.txt").read_text(encoding="utf-8") == "from UI"


@pytest.mark.asyncio
def test_memory_follows_git_not_a_side_channel():
    """记忆住在 worktree 里，因此**跟着 Git 分支走**。

    两个入口各在自己的 `session/<id>` 分支上，各自写的教训在合回主线之前
    互相看不见 —— 这跟 artifact、KB 的语义一致，reconcile 是 Git 的活。

    上一代不是这样：候选队列在 worktree **外**（`projects/<id>/memory/`）、
    MEMORY.md 在 worktree 里，于是同一份记忆有两个权威、各自演化。
    把队列搬回 worktree 外能让"两个 session 立刻互见"，但那正是被删掉的
    第二权威 —— 用一次分叉换一点即时可见性，代价这个仓库已经付过了。
    """
    from core import memory as M
    from core.project_bootstrap import open_session_worktree
    from core import paths as _paths
    from core.state import State

    base = _paths.home() / "runs"
    ui_tree = open_session_worktree("demo", "ui-abc")
    cli_tree = open_session_worktree("demo", "cli-xyz")
    assert ui_tree != cli_tree

    ui_run = State.new(node_type="literature", base_dir=base,
                       project_id="demo", project_worktree=ui_tree)
    M.ensure_skeleton(ui_run)
    M.append_manual(ui_run, text="这条经验是在 UI 会话里记下的",
                    section=M.SECTION_PITFALL, nodes=["literature"], run_id="r1")

    # 每个 session 的记忆锚在自己那棵树上 —— 路径不同、互不覆盖
    cli_run = State.new(node_type="_curator", base_dir=base,
                        project_id="demo", project_worktree=cli_tree)
    assert M.memory_path(ui_run) != M.memory_path(cli_run)
    assert "UI 会话里记下的" in M.read_section(ui_run, M.SECTION_PITFALL)



