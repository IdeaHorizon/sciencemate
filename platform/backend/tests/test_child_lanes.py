"""子 lane：派一个子节点就给它一棵自己的树（RFC 异步运行时 P2-a / D4 / D5）。

## 这一片解决的是什么

「派活之后立刻返回」在今天做不到，卡点在 Git 而不在调度：一个 Session
worktree 只有一条 mutation lane，后台 child 会与父节点、与平台 checkpoint
并发改同一棵树。`run_node` 里那道 `background + project_worktree → error`
的禁令就是这么来的。

lane 把那个前提拆掉：每个被派出去的子节点拿一棵自己的工作树 + 分支，跑完
机械 merge 回 session 分支。

## ⚠️ RFC 的 D5 在一个承重点上和代码对不上

D5 原文是「子分支跑完 → **调度器侧**机械 merge」。核对之后不成立：worker
全程不碰 git —— 节点写完文件只发一条 `workspace_checkpoint_requested`，
真正 commit 的是 App Server（`checkpoint_session_workspace` 的 docstring
原话："The node reports paths but never receives Git authority."）。

所以 lane 的三个动作都是**平台**的动作。这不是实现口味，是既有的权限边界：
把 merge 交给调度器等于让节点能改历史。

## 为什么这些测试起真 git

lane 的每一条性质都是 Git 的性质（分支从哪里长、冲突算不算冲突、merge
commit 是谁做的）。用替身测等于自己给自己出题 —— 那正是「替身遮住被测实现」。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services.project_repository import (
    ChildLaneConflictError,
    GitProjectRepository,
    ProjectRepositoryError,
)


@pytest.fixture
def repository(tmp_path: Path) -> GitProjectRepository:
    return GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")


@pytest.fixture
def session(repository: GitProjectRepository):
    repository.initialize_project(
        project_id="p1", name="Lanes", description=None,
        research_domain=None, owner_id="u1",
    )
    return repository.ensure_session_workspace(
        project_id="p1", session_id="s1", base_commit=None,
        title="Lane session", created_by="u1",
    )


def _write_and_commit(root: Path, relative: str, content: str, message: str) -> None:
    """替 `checkpoint_session_workspace` 在 lane 里落一条节点提交。

    ⚠️ 必须带 `Session-ID` trailer —— 那是"这条提交是平台为这个 session 做的"
    的机械标记（见 `_commits_not_made_by_platform`）。真实的 checkpoint 一直
    在写它；替身少写了就不是替身，是一个更宽松的世界，而在那个世界里 D6 那条
    测试永远绿。
    """
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", relative], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message, "-m", "Session-ID: s1"],
        cwd=root, check=True, capture_output=True,
    )


# ── 开 lane ────────────────────────────────────────────────────────────────

def test_a_lane_is_its_own_worktree_branched_from_the_session_head(repository, session):
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    assert Path(lane.path).is_dir()
    assert lane.base_commit == session.head_commit
    assert lane.branch == "lane/s1/l1"
    # 独立的树：在 lane 里写东西，session 树看不见。
    (Path(lane.path) / "experiments" / "note.md").write_text("lane work", encoding="utf-8")
    assert not (Path(session.path) / "experiments" / "note.md").exists()


def test_the_lane_worktree_lives_outside_the_session_worktree(repository, session):
    """放进 session 树里面，Git 会把整棵 lane 当成 session 的未跟踪垃圾 ——
    而 checkpoint 的脏路径枚举是 `--untracked-files=all`。"""
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    assert not Path(lane.path).is_relative_to(Path(session.path))


def test_open_lanes_are_derived_from_git_not_from_a_second_registry(repository, session):
    """"现在有哪些 lane" 的答案就是 Git 自己。

    另存一份注册表，它与磁盘的分叉是迟早的事（worktree 被手工删掉、分支被
    改名…），而分叉时两边都不报错。
    """
    repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l2", node_type="writing"
    )
    lanes = {lane.lane_id: lane.node_type
             for lane in repository.open_child_lanes("p1", "s1")}
    assert lanes == {"l1": "experiment", "l2": "writing"}


def test_reopening_the_same_lane_is_idempotent(repository, session):
    first = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    again = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    assert again.branch == first.branch and again.path == first.path


def test_two_lanes_for_one_node_are_refused_at_dispatch(repository, session):
    """同节点并行机械禁令（D5）。

    两条 lane 会改同一个节点目录，合第二条**必然**冲突。判据机械可答，就该
    在派发那一刻撞墙 —— 比让它跑完一小时再在 merge 处炸掉便宜得多。
    """
    repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    with pytest.raises(ProjectRepositoryError, match="已经有一条在跑的 lane"):
        repository.open_child_lane(
            project_id="p1", session_id="s1", lane_id="l2", node_type="experiment"
        )


def test_a_lane_id_cannot_be_reused_for_a_different_node(repository, session):
    repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    with pytest.raises(ProjectRepositoryError, match="already belongs to node"):
        repository.open_child_lane(
            project_id="p1", session_id="s1", lane_id="l1", node_type="writing"
        )


# ── 合 lane ────────────────────────────────────────────────────────────────

def test_a_clean_lane_lands_on_the_session_branch(repository, session):
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    _write_and_commit(Path(lane.path), "experiments/result.md", "42\n", "node: result")

    merged = repository.merge_child_lane(project_id="p1", session_id="s1", lane_id="l1")
    assert merged.merge_commit is not None
    assert merged.paths == ("experiments/result.md",)
    assert (Path(session.path) / "experiments/result.md").read_text() == "42\n"


def test_a_lane_that_produced_nothing_is_not_an_error(repository, session):
    """子节点跑了但没写文件是合法的（报了 blocker、或者结论就是"不用改"）。"""
    repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    merged = repository.merge_child_lane(project_id="p1", session_id="s1", lane_id="l1")
    assert merged.merge_commit is None and merged.paths == ()


def test_two_lanes_on_different_nodes_both_land(repository, session):
    """所有制即无冲突 —— 这是 lane 能存在的前提，所以要有一条正面证据。"""
    first = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    second = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l2", node_type="writing"
    )
    _write_and_commit(Path(first.path), "experiments/a.md", "A\n", "node: a")
    _write_and_commit(Path(second.path), "paper/b.md", "B\n", "node: b")

    repository.merge_child_lane(project_id="p1", session_id="s1", lane_id="l1")
    repository.merge_child_lane(project_id="p1", session_id="s1", lane_id="l2")
    assert (Path(session.path) / "experiments/a.md").read_text() == "A\n"
    assert (Path(session.path) / "paper/b.md").read_text() == "B\n"


def test_a_real_conflict_is_refused_and_names_the_files(repository, session):
    """冲突**不自动解**（D5）。

    六节点目录所有制保证正常情况下零冲突，所以真冲突是"所有制被破坏了"的
    信号。自动解就是「无害的两段能组合出销毁」那个形状：每一步都合理，合起来
    把一份研究产物改成谁也没写过的样子，而且没人会知道。
    """
    # 两条不同节点的 lane，但都去改同一个文件 —— 所有制被破坏的样子。
    first = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    second = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l2", node_type="writing"
    )
    _write_and_commit(Path(first.path), "shared/note.md", "from experiment\n", "a")
    _write_and_commit(Path(second.path), "shared/note.md", "from writing\n", "b")

    repository.merge_child_lane(project_id="p1", session_id="s1", lane_id="l1")
    with pytest.raises(ChildLaneConflictError) as error:
        repository.merge_child_lane(project_id="p1", session_id="s1", lane_id="l2")
    assert error.value.paths == ("shared/note.md",)
    # 冲突之后 session 树必须是干净的 —— merge 被 abort 掉，不许留半个合并态。
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=session.path, capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == ""
    assert (Path(session.path) / "shared/note.md").read_text() == "from experiment\n"


def test_a_lane_may_not_land_a_shared_single_writer_file(repository, session):
    """`MEMORY.md` 不属于任何节点目录，所有制保证不了零冲突。

    所以它不走 merge：改动随完成回报交 proposal，由调度器单写落盘 ——
    与 KB「无直接写路径」同构。这里守的是**机械那一半**：带着它来就拒。
    """
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    _write_and_commit(Path(lane.path), "MEMORY.md", "child edited memory\n", "memory")
    with pytest.raises(ChildLaneConflictError) as error:
        repository.merge_child_lane(project_id="p1", session_id="s1", lane_id="l1")
    assert error.value.paths == ("MEMORY.md",)


def test_uncommitted_lane_work_is_refused_rather_than_committed_for_it(repository, session):
    """谁写的谁负责入库。

    代提交等于把"这段产物属于哪个 run"变成猜的 —— 而那正是 checkpoint
    trailer 存在的理由。
    """
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    (Path(lane.path) / "experiments" / "wip.md").write_text("half done", encoding="utf-8")
    with pytest.raises(ProjectRepositoryError, match="uncommitted work"):
        repository.merge_child_lane(project_id="p1", session_id="s1", lane_id="l1")


# ── D6：merge commit 必须是"自己人" ────────────────────────────────────────

def test_the_merge_commit_does_not_trip_checkpoint_authority(repository, session):
    """RFC D6：merge commit 用平台身份提交，权威判定不得 fail-closed。

    漏这条的症状是老朋友：「平台把自己的提交判成外人 → 永久拒绝续跑」
    （2026-08-13 E2E v26，一条 run 因此在 8.9 小时模拟跑完前就死了）。
    """
    before = session.head_commit
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    _write_and_commit(Path(lane.path), "experiments/result.md", "42\n", "node: result")
    merged = repository.merge_child_lane(project_id="p1", session_id="s1", lane_id="l1")

    foreign = repository._commits_not_made_by_platform(
        Path(session.path), expected=before, current=merged.session_head, session_id="s1"
    )
    assert foreign == [], (
        f"merge commit 被自己的权威判据当成外人了：{foreign} —— "
        "下一次 checkpoint 会 fail-closed，会话再也自己好不了"
    )


# ── 撤 lane ────────────────────────────────────────────────────────────────

def test_closing_a_lane_removes_the_worktree(repository, session):
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    repository.close_child_lane(project_id="p1", session_id="s1", lane_id="l1")
    assert not Path(lane.path).exists()
    assert repository.open_child_lanes("p1", "s1") == ()


def test_abandoning_a_lane_keeps_its_history(repository, session):
    """放弃 ≠ 销毁。「只留证据不判决」—— 那段工作还得翻得出来。"""
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    _write_and_commit(Path(lane.path), "experiments/abandoned.md", "x\n", "node: wip")
    repository.close_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", delete_branch=False
    )
    assert not Path(lane.path).exists()
    kept = subprocess.run(
        ["git", "show", f"{lane.branch}:experiments/abandoned.md"],
        cwd=repository.project_path("p1"), capture_output=True, text=True, check=True,
    )
    assert kept.stdout == "x\n"


def test_a_closed_lane_frees_its_node_for_the_next_dispatch(repository, session):
    """同节点禁令是"同时"，不是"永远"。"""
    repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    repository.close_child_lane(project_id="p1", session_id="s1", lane_id="l1")
    repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l2", node_type="experiment"
    )


# ── 落 lane：节点的 checkpoint 进的是自己那条分支 ──────────────────────────

def test_a_node_checkpoint_lands_on_its_own_lane(repository, session):
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    (Path(lane.path) / "experiments" / "result.md").write_text("42\n", encoding="utf-8")
    checkpoint = repository.checkpoint_child_lane(
        project_id="p1", session_id="s1", lane_id="l1",
        node_type="experiment", run_id="run-1", run_status="completed",
        workspace_prefix="experiments", paths=["experiments/result.md"],
    )
    assert checkpoint.commit_sha is not None
    assert checkpoint.branch == "lane/s1/l1"
    # session 分支还没看到它 —— 落地是 merge 的事。
    assert not (Path(session.path) / "experiments/result.md").exists()


def test_a_lane_checkpoint_then_merge_reaches_the_session(repository, session):
    """两个原语接起来：落 → 合。这条是 P2-b 会走的那条真实路径。"""
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    (Path(lane.path) / "experiments" / "result.md").write_text("42\n", encoding="utf-8")
    repository.checkpoint_child_lane(
        project_id="p1", session_id="s1", lane_id="l1",
        node_type="experiment", run_id="run-1", run_status="completed",
        workspace_prefix="experiments", paths=["experiments/result.md"],
    )
    merged = repository.merge_child_lane(project_id="p1", session_id="s1", lane_id="l1")
    assert merged.paths == ("experiments/result.md",)
    assert (Path(session.path) / "experiments/result.md").read_text() == "42\n"


def test_the_lane_checkpoint_is_the_same_implementation_as_the_session_one(
    repository, session
):
    """两条到达路径**同一份实现** —— 各写一份必然分叉成"父子跑出来的历史
    长得不一样"，而两边都不报错。

    判据不看源码写法，看**结果**：lane 的提交必须带着 session checkpoint
    那一套 trailer（D6 的权威链就靠它）。
    """
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    (Path(lane.path) / "experiments" / "result.md").write_text("42\n", encoding="utf-8")
    repository.checkpoint_child_lane(
        project_id="p1", session_id="s1", lane_id="l1",
        node_type="experiment", run_id="run-1", run_status="completed",
        workspace_prefix="experiments", paths=["experiments/result.md"],
    )
    body = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=lane.path, capture_output=True, text=True, check=True,
    ).stdout
    assert "Session-ID: s1" in body
    assert "Node-Type: experiment" in body
    assert "Run-ID: run-1" in body


def test_a_lane_checkpoint_refuses_writes_outside_the_node_workspace(repository, session):
    """写边界的第二道见证在 lane 里同样在场（同一份 `_validate_checkpoint_paths`）。"""
    lane = repository.open_child_lane(
        project_id="p1", session_id="s1", lane_id="l1", node_type="experiment"
    )
    (Path(lane.path) / "paper").mkdir(exist_ok=True)
    (Path(lane.path) / "paper" / "sneaky.md").write_text("not mine\n", encoding="utf-8")
    with pytest.raises(ProjectRepositoryError):
        repository.checkpoint_child_lane(
            project_id="p1", session_id="s1", lane_id="l1",
            node_type="experiment", run_id="run-1", run_status="completed",
            workspace_prefix="experiments", paths=["paper/sneaky.md"],
        )


def test_checkpointing_a_lane_that_is_not_open_is_a_clear_error(repository, session):
    with pytest.raises(ProjectRepositoryError, match="not open"):
        repository.checkpoint_child_lane(
            project_id="p1", session_id="s1", lane_id="nope",
            node_type="experiment", run_id="run-1", run_status="completed",
            workspace_prefix="experiments", paths=["experiments/x.md"],
        )
