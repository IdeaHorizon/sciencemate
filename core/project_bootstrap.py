"""给一个项目准备好它的 Project Git 工作区 —— CLI 和平台用同一份骨架。

## 为什么需要它

Project Git workspace（v2.1）是现行架构：节点各拥有一个目录、产物就是文件、
MEMORY.md 是唯一的记忆权威。但接通它的只有平台侧 —— `chat.py` 的 CLI 入口
调 `_make_or_load_orchestrator_state` 时**不传 `project_worktree`**，于是
全仓 70+ 处 `if state.project_worktree is not None:` 在 CLI 下永远走 else。

那些 else 分支是 v1 遗留。两条路都活着的后果不是"兼容"，是同一个功能有两种
行为，而且只有一种被维护：实测 CLI 项目的 `memory/` 目录里 87 条记忆，平台
项目一条都没有 —— 因为记忆的持久化整条都写在 else 分支里，`if` 那边只留了
"报给 orchestration"就返回。

所以这里不是"给 CLI 加个新功能"，是把 CLI 接到**已经是现行架构的那条路**上，
好让那些 else 分支能被删掉。

## 与平台侧的关系

平台的 `ProjectRepository` 做的更多（裸仓 + 每 session 一个 worktree + 分支 +
change-set 审核）。CLI 不需要那套多人协作机制，它只要一个"这个项目的工作区"。
两边的**骨架必须一致** —— 目录名就是所有权表（`core.project_workspace.
_NODE_WORKSPACES`）的键，不一致就等于换个入口进来所有权判定全变。所以骨架
定义在这里，平台那份保持它自己的建仓流程但用同一组名字。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from core import paths as _paths

from core.project_workspace import producing_node_dirs, system_node_dirs

# 节点自己的目录 —— 从 core.project_workspace._NODE_WORKSPACES **推导**，不抄。
# 此前这里抄了一份，靠一条测试钉住两边相等；抄件迟早分叉，且分叉时两边都不报错。
_NODE_DIRS = producing_node_dirs()
_SYSTEM_DIRS = system_node_dirs()

_MEMORY_MD = (
    "# Project memory\n\n"
    "This is the canonical, curated memory for the research Project. "
    "Keep entries concise and evidence-linked; compact superseded entries "
    "instead of creating a second memory authority.\n\n"
    "## Goals\n\n- Establish the Project's measurable research goals.\n\n"
    "## Decisions\n\n"
    "<!-- YYYY-MM-DD | decision | rationale | evidence/commit -->\n\n"
    "## Findings\n\n"
    "<!-- YYYY-MM-DD | finding | confidence | evidence path -->\n\n"
    "## Constraints\n\n"
    "<!-- stable constraints that affect research execution -->\n\n"
    "## Open questions\n\n"
    "<!-- unresolved questions and the next evidence needed -->\n"
)

_GITIGNORE = (
    "# 运行事实存档 —— 不进版本，但**不是缓存**：一次研究到底发生了什么\n"
    "# （transcript / events / checkpoint / 工具原文）全部在这下面，删掉就是\n"
    "# 销毁科研过程记录，没有任何一步能把它重算回来。\n"
    ".research/runtime/\n"
    "# 2026-08-27 之前它叫 cache；存量会话还在用，两条都忽略。\n"
    ".research/cache/\n"
)


class ProjectBootstrapError(RuntimeError):
    """无法为这个项目准备工作区。"""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )
    if result.returncode != 0:
        raise ProjectBootstrapError(
            f"git {' '.join(args)} failed in {root}: {result.stderr.strip()[:300]}"
        )
    return result.stdout


def session_worktree_path(project_id: str, session_id: str) -> Path | None:
    """`projects/<id>/sessions/<session_id>/` —— 一次会话自己的工作区。

    布局与平台的 `ProjectRepository.session_path` 同构（`<root>/<project>/<session>`）
    ——两边都是"一个项目，多个并行会话"，用同一种形状，换个入口打开同一个项目
    才谈得上。
    """
    project_dir = _paths.project_dir(project_id)
    return None if project_dir is None else project_dir / "sessions" / session_id


def session_branch(session_id: str) -> str:
    """会话分支名 —— 与平台 `ProjectRepository.session_branch` 同一约定。"""
    return f"session/{session_id}"


def open_session_worktree(project_id: str, session_id: str) -> Path:
    """给一次会话开一个自己的 worktree（幂等）。

    为什么不是直接改项目主工作区：一个 worktree 只有一条 Git mutation lane。
    UI 已经在跑的 session 和 CLI 同时改同一棵树，就是两个写者抢一条道 ——
    平台为此专门禁掉了 background child（见 `run_node`）。

    每个入口开自己的 worktree、各自在 `session/<id>` 分支上，就没有这个问题：
    "两个入口打开同一个项目"成立，而且互不打架。合回主线走跟 UI 一样的路径。
    """
    if not project_id or not session_id:
        raise ProjectBootstrapError("project_id 和 session_id 都不能为空")
    repo = ensure_project_worktree(project_id)
    if repo is None:
        raise ProjectBootstrapError(f"项目 {project_id!r} 没有工作区")
    path = session_worktree_path(project_id, session_id)
    assert path is not None
    if (path / ".git").exists():
        return path

    branch = session_branch(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", f"refs/heads/{branch}"],
        capture_output=True, text=True, check=False,
    ).returncode == 0
    if existing:
        _git(repo, "worktree", "add", str(path), branch)
    else:
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path


def project_worktree_path(project_id: str) -> Path | None:
    """`projects/<id>/workspace/` —— 项目的工作区落点。"""
    project_dir = _paths.project_dir(project_id)
    return None if project_dir is None else project_dir / "workspace"


def ensure_project_worktree(project_id: str | None) -> Path | None:
    """确保这个项目有一个可用的 Project Git 工作区，返回它的路径。

    幂等：已经是 git 仓就直接返回，不动里面任何东西（**绝不** re-init —— 那会
    把已有历史变成一个新仓，节点产物的 provenance 全断）。

    `project_id` 为空（ad-hoc run）返回 None —— 那种 run 本来就没有项目级持久
    状态，绑 worktree 无意义。
    """
    if not project_id:
        return None
    root = project_worktree_path(project_id)
    if root is None:
        return None

    if (root / ".git").exists():
        return root

    root.mkdir(parents=True, exist_ok=True)
    # 目录非空但不是 git 仓 —— 说明有人（或旧版本）在这儿放过东西。
    # 直接 init 会把它们一并纳入一个凭空出现的初始提交，来路不明。宁可吵。
    existing = [p for p in root.iterdir() if p.name != ".git"]
    if existing:
        raise ProjectBootstrapError(
            f"{root} 已有内容但不是 Git 仓库（{len(existing)} 项）。"
            f"先确认这些文件的来路：要么手工 git init 并提交，要么移走。"
        )

    _git(root, "init", "-b", "main")
    for directory in (*_NODE_DIRS, *_SYSTEM_DIRS):
        d = root / directory
        d.mkdir(parents=True, exist_ok=True)
        # git 不跟踪空目录 —— 没有 .gitkeep 的话骨架一提交就没了，
        # 而所有权判定认的是目录存在。
        (d / ".gitkeep").write_text("", encoding="utf-8")
    (root / "MEMORY.md").write_text(_MEMORY_MD, encoding="utf-8")
    (root / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")

    _git(root, "add", "-A")
    _git(root, "-c", "user.name=harness", "-c", "user.email=harness@local",
         "commit", "-m", "project: initialize research workspace")
    return root
