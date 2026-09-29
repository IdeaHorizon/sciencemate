"""Windows 的路径预算是硬的，会话路径必须放得进去（2026-09-10 真机事故的验收）。

## 那天发生了什么

Windows 桌面版装好、后端起来、控制面通了之后，**每一个会话都在 init 那一步崩**：

    [WinError 206] The filename or extension is too long:
    C:\\Users\\<user>\\AppData\\Local\\afs\\project-worktrees\\<uuid36>\\<uuid36>\\
    .research\\runtime\\runs\\orchestrator__<uuid36>__session__<uuid36>\\artifacts

255 字符。`CreateDirectory` 的上限是 **MAX_PATH − 12 = 248**（要给目录里的 8.3
文件名留位置），不是 260。同一个数据根里最深的文件已经 258。

病根是**同一对 id 在这条路径里各出现两次**：光目录名就吃掉 145 个字符。

## 这条测的是什么

不是"名字变短了"（那只是手段），是**真实布局算出来的最深路径放得进 Windows 的
预算**。三段都用生产代码现算 —— 工作树位置（`session_path`/`lane_path`）、运行时
根（`session_runs_root`）、run 目录名（`_RUNTIME_DIR_TEMPLATE`）——所以将来谁把
其中任何一段加长，这条会当场红，而不是等一台 Windows 机器上的用户来报。

尾巴长度是**真机量出来的**，不是估的（9800x3d，真跑过的数据根）：
run 目录下最深 24（`messages_checkpoint.json`）、工作树根下最深的是
`execution_envelope__sha256_<64hex>.json`。量的那天它还在信封布局的
`experiments/artifacts/` 下（92）；记录改成原生文件之后（RFC 2026-09-12 §6，
没有 `artifacts/` 子目录）同一份东西是 `experiments/execution_envelope__sha256_<64hex>.json`
（82）——这里按现行布局算，判据不变。
"""
from __future__ import annotations

import uuid

import pytest

from app.services import project_repository as pr
from app.services.harness_sessions import _RUNTIME_DIR_TEMPLATE
from app.services.session_runtime_paths import session_runs_root

#: `CreateDirectory` 的上限：MAX_PATH − 12（目录里还要放得下一个 8.3 名字）。
WINDOWS_DIR_LIMIT = 248
#: 文件路径的上限。
WINDOWS_PATH_LIMIT = 260

#: 干净机器上的数据根（`core.paths.default_home()` 在 Windows 的答案）。
WINDOWS_DATA_ROOT = "C:\\Users\\fcbay\\AppData\\Local\\afs"

#: 真机量到的最深尾巴。
DEEPEST_TAIL_UNDER_RUN_DIR = len("messages_checkpoint.json")
DEEPEST_TAIL_UNDER_WORKTREE = len(
    "experiments/execution_envelope__sha256_" + "0" * 64 + ".json"
)


#: 工作树根在干净 Windows 机器上的样子（真机实测的那一个）。
WINDOWS_WORKTREE_ROOT = WINDOWS_DATA_ROOT + "\\project-worktrees"


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """在**任何**宿主上问「Windows 上会算成什么」——判据不许依赖运行环境。

    根用宿主的临时目录（`GitProjectRepository` 会 `resolve()` 它，宿主上得是
    真路径），量长度时再换成 Windows 上的真实根：布局那一段由生产代码算，根
    那一段是常量，两者相加才是那台机器上的真实长度。
    """
    monkeypatch.setattr(pr, "_WINDOWS", True)
    root = tmp_path / "wt"
    repo = pr.GitProjectRepository(repository_root=tmp_path / "repo", worktree_root=root)
    return repo


def _windows_length(path, repo) -> int:
    """这条路径在真实 Windows 机器上有多少个字符。"""
    relative = str(path.relative_to(repo.worktree_root)).replace("/", "\\")
    return len(WINDOWS_WORKTREE_ROOT) + 1 + len(relative)


def test_the_deepest_session_runtime_path_fits(windows):
    """会话的运行时记录 —— 那天真正炸掉的那一条。"""
    project_id, session_id = str(uuid.uuid4()), str(uuid.uuid4())
    worktree = windows.session_path(project_id, session_id)
    run_dir = session_runs_root(worktree) / _RUNTIME_DIR_TEMPLATE.format(
        project_id=project_id, session_id=session_id
    )
    artifacts = run_dir / "artifacts"

    assert _windows_length(artifacts, windows) <= WINDOWS_DIR_LIMIT, (
        f"要建的目录 {_windows_length(artifacts, windows)} 字符 > {WINDOWS_DIR_LIMIT}："
        f"Windows 上每个会话都会在 init 崩掉\n{artifacts}"
    )
    deepest_file = _windows_length(artifacts, windows) + 1 + DEEPEST_TAIL_UNDER_RUN_DIR
    assert deepest_file <= WINDOWS_PATH_LIMIT, deepest_file


def test_the_deepest_workspace_artifact_fits(windows):
    """模型自己产出的东西落在工作树根下 —— 那一头也得放得下。"""
    worktree = windows.session_path(str(uuid.uuid4()), str(uuid.uuid4()))
    deepest = _windows_length(worktree, windows) + 1 + DEEPEST_TAIL_UNDER_WORKTREE
    assert deepest <= WINDOWS_PATH_LIMIT, (
        f"工作树根 {_windows_length(worktree, windows)} + 最深产物 "
        f"{DEEPEST_TAIL_UNDER_WORKTREE} = {deepest} > {WINDOWS_PATH_LIMIT}"
    )


def test_the_deepest_lane_runtime_path_fits(windows):
    """lane 工作树比 session 更深（名字里带着 session + lane 两个 id）。"""
    project_id, session_id, lane_id = (str(uuid.uuid4()) for _ in range(3))
    lane = windows.lane_path(project_id, session_id, lane_id)
    run_dir = session_runs_root(lane) / _RUNTIME_DIR_TEMPLATE.format(
        project_id=project_id, session_id=session_id
    )
    assert _windows_length(run_dir / "artifacts", windows) <= WINDOWS_DIR_LIMIT, run_dir


def test_posix_keeps_the_identifiers_verbatim():
    """POSIX 那边一个字都不动：那里没有预算问题，而给活着的工作树改名要
    `git worktree repair` —— 拿真实的迁移风险换零收益。"""
    assert pr._worktree_dir_name("46358af4-6e53-40fd-9df5-4169b40bdc97") == (
        "46358af4-6e53-40fd-9df5-4169b40bdc97"
    )


def test_the_short_name_is_a_function_of_the_identity_only(monkeypatch):
    """短名必须只由身份决定 —— 同一个会话每次算出来都得是同一个目录，
    否则「现算路径」这条设计当场失效。"""
    monkeypatch.setattr(pr, "_WINDOWS", True)
    identifier = "46358af4-6e53-40fd-9df5-4169b40bdc97"
    first = pr._worktree_dir_name(identifier)
    assert first == pr._worktree_dir_name(identifier)
    assert first != pr._worktree_dir_name("59541cfa-8046-4efe-99f0-3aecb881b02f")
    assert len(first) == pr._WORKTREE_DIR_HEX


def test_a_short_name_collision_is_refused_not_shared(tmp_path, monkeypatch):
    """短名撞车是天文数字级的小概率 —— 但撞了必须**当场说出来**。

    静默地让两个会话共写一棵工作树，两边都不会报错：一个会话的提交落在另一个的
    分支上，而 UI 上两边都显示正常。这正是本仓库反复栽过的那种形状。

    这里把短名强行压成常量来构造碰撞（真实碰撞造不出来），判据是它**拒绝**。
    """
    monkeypatch.setattr(pr, "_worktree_dir_name", lambda _identifier: "collide")
    repo = pr.GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")
    project = repo.initialize_project(
        project_id="project-collision",
        name="Project",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    repo.ensure_session_workspace(
        project_id="project-collision",
        session_id="session-first",
        base_commit=project.head_commit,
        title="First",
        created_by="owner",
    )
    with pytest.raises(pr.ProjectRepositoryError, match="already belongs to branch"):
        repo.ensure_session_workspace(
            project_id="project-collision",
            session_id="session-second",
            base_commit=project.head_commit,
            title="Second",
            created_by="owner",
        )


def test_both_worktree_paths_use_the_same_naming_rule(monkeypatch):
    """session 与 lane 是同一棵树上的两种工作树 —— 名字的规则只有一份。

    抄一份的后果不是报错，是 lane 落在一个**没被算过预算**的位置上。
    """
    monkeypatch.setattr(pr, "_WINDOWS", True)
    repo = pr.GitProjectRepository(
        repository_root=pr.Path("/tmp/afs-repo"), worktree_root=pr.Path("/tmp/afs-wt")
    )
    project_id, session_id, lane_id = (str(uuid.uuid4()) for _ in range(3))
    session = repo.session_path(project_id, session_id)
    lane = repo.lane_path(project_id, session_id, lane_id)
    assert lane.parent == session.parent
    assert lane.name.startswith(pr._worktree_dir_name(session_id) + "__lane__")
    assert session.name == pr._worktree_dir_name(session_id)
    assert session.parent.name == pr._worktree_dir_name(project_id)
