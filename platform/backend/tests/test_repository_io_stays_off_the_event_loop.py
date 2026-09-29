"""仓库 I/O 不许跑在事件循环线程上，而且读侧的代价不许随工作区体量增长。

## 现场（2026-09-09 node20，会话 6c211c70…c2db10da）

observation 节点把一份日志 tar 解成 4492 个文件（897MB，两份副本），checkpoint
全部收进会话分支。之后每一次「这个会话改了什么」都要：生成整份补丁（1.83GB）
再截到 200KB，对每个改动文件读 1MB 样本扫密钥（1.5GB）——同步跑在 uvicorn 的
主线程上，一次 35 秒；而它挂在会话列表 5 秒一次的轮询上。整台后端以 35 秒为
量子停摆：GET 34～145 秒，事件摄取每条间隔 35 秒，一张决策卡从产生到入账 13 分钟。

## 三条不变量，各一道闸

1. `_git` 在事件循环线程上被调用 → 当场抛，不是变慢。合法的路只有
   `run_in_repository_thread`。
2. 后端里每一处会跑 git 的仓库方法调用都走那条路（AST 扫盘，不写名单）。
3. 补丁按预算出：读多少算多少，git 调用次数不随改动文件数增长。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from app.services import project_repository as module
from app.services.project_repository import (
    GitProjectRepository,
    ProjectRepositoryError,
    run_in_repository_thread,
)

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"


def _repository(tmp_path: pathlib.Path) -> GitProjectRepository:
    return GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")


# ── 1. 闸在唯一的出口 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_git_refuses_to_run_on_the_event_loop_thread(tmp_path: pathlib.Path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ProjectRepositoryError, match="event loop thread"):
        repository.initialize_project(
            project_id="p", name="P", description=None, research_domain=None, owner_id="u"
        )


@pytest.mark.asyncio
async def test_the_repository_thread_is_the_legal_path(tmp_path: pathlib.Path) -> None:
    repository = _repository(tmp_path)
    status = await run_in_repository_thread(
        repository.initialize_project,
        project_id="p", name="P", description=None, research_domain=None, owner_id="u",
    )
    assert status.head_commit


def test_git_runs_normally_off_the_loop(tmp_path: pathlib.Path) -> None:
    """没有事件循环的线程（CLI、pytest 的同步测试、工作线程）照常。"""
    repository = _repository(tmp_path)
    status = repository.initialize_project(
        project_id="p", name="P", description=None, research_domain=None, owner_id="u"
    )
    assert status.head_commit


# ── 2. 调用点扫盘 ───────────────────────────────────────────────────────────


def _methods_that_touch_git() -> set[str]:
    """`GitProjectRepository` 里哪些方法会跑 git —— 从源码推，不写名单。

    判据：方法体里出现 `self._git(` / `self._git_exit_code(` / `self._project_lock(`，
    或调用了另一个这样的方法（传递闭包）。
    """
    tree = ast.parse((_APP / "services" / "project_repository.py").read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GitProjectRepository"
    )
    calls: dict[str, set[str]] = {}
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called: set[str] = set()
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                called.add(node.func.attr)
        calls[fn.name] = called
    touching = {"_git", "_git_exit_code", "_project_lock"}
    changed = True
    while changed:
        changed = False
        for name, called in calls.items():
            if name not in touching and called & touching:
                touching.add(name)
                changed = True
    return touching - {"_git", "_git_exit_code", "_project_lock"}


def _offending_calls(source: pathlib.Path, git_methods: set[str]) -> list[str]:
    """async 函数体里直接调了会跑 git 的仓库方法、且没有包在 run_in_repository_thread 里。"""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    wrapped: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_in_repository_thread"
        ):
            for inner in ast.walk(node):
                wrapped.add(id(inner))
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in git_methods or id(node) in wrapped:
                continue
            shown = source.relative_to(_BACKEND) if source.is_relative_to(_BACKEND) else source
            offenders.append(f"{shown}:{node.lineno} {node.func.attr}()")
    return offenders


def test_no_async_code_calls_git_backed_repository_methods_directly() -> None:
    git_methods = _methods_that_touch_git()
    assert {"diff", "session_status", "checkpoint_session_workspace"} <= git_methods, (
        "扫描本身失效了：这几个方法明明跑 git"
    )
    offenders: list[str] = []
    for source in sorted(_APP.rglob("*.py")):
        offenders.extend(_offending_calls(source, git_methods))
    assert not offenders, (
        "这些 async 调用点在事件循环上直接跑 git —— 一处慢就是整台后端慢：\n"
        + "\n".join(offenders)
    )


def test_the_call_site_scan_is_not_vacuous(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "async def handler(repo):\n"
        "    return repo.session_status('p', 's')\n",
        encoding="utf-8",
    )
    found = _offending_calls(sample, {"session_status"})
    assert len(found) == 1 and found[0].endswith(":2 session_status()"), found


# ── 3. 补丁按预算出 ─────────────────────────────────────────────────────────


def test_the_patch_costs_its_budget_not_the_worktree(tmp_path: pathlib.Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="p", name="P", description=None, research_domain=None, owner_id="u"
    )
    session = repository.ensure_session_workspace(
        project_id="p", session_id="s", base_commit=project.head_commit,
        title="S", created_by="u",
    )
    root = pathlib.Path(session.path)
    data = root / "data" / "extracted"
    data.mkdir(parents=True)
    for index in range(400):
        (data / f"log_{index:04d}.txt").write_text(f"line {index}\n" * 40, encoding="utf-8")

    calls: list[tuple[str, ...]] = []
    original = GitProjectRepository._git

    def counting(self, cwd, *args, **kwargs):
        calls.append(tuple(args[:2]))
        return original(self, cwd, *args, **kwargs)

    monkeypatch.setattr(GitProjectRepository, "_git", counting)
    diff = repository.diff(project_id="p", session_id="s", max_bytes=4_000)

    assert diff.truncated, "400 个文件的补丁不可能塞进 4KB"
    assert 0 < diff.files_changed < 400, "只描述读到的那一段"
    assert len(diff.patch.encode("utf-8")) < 4_000 + 200
    per_file = [args for args in calls if args and args[0] == "diff" and len(args) > 1]
    assert len(calls) < 40, (
        f"git 被调了 {len(calls)} 次 —— 代价跟着 400 个文件走了，预算没起作用"
    )
    assert per_file, "补丁必须逐文件要，才停得下来"


def test_changed_paths_read_no_content(tmp_path: pathlib.Path, monkeypatch) -> None:
    """「改了多少文件」= 名单长度。这一问不许读任何文件内容。"""
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="p", name="P", description=None, research_domain=None, owner_id="u"
    )
    session = repository.ensure_session_workspace(
        project_id="p", session_id="s", base_commit=project.head_commit,
        title="S", created_by="u",
    )
    root = pathlib.Path(session.path)
    (root / "data").mkdir(exist_ok=True)
    for index in range(50):
        (root / "data" / f"f{index}.txt").write_text("x" * 10_000, encoding="utf-8")
    repository.checkpoint_session_workspace(
        project_id="p", session_id="s", node_type="data", run_id="r", run_status="completed",
        workspace_prefix="data", paths=[f"data/f{i}.txt" for i in range(50)],
        expected_head_commit=session.head_commit,
    )
    opened: list[str] = []
    real_open = pathlib.Path.open

    def spying_open(self, *args, **kwargs):
        if self.suffix == ".txt":
            opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", spying_open)
    assert len(repository.changed_paths("p", "s")) == 50
    assert opened == [], "计数读了文件内容 —— 代价又跟体量走了"


@pytest.mark.asyncio
async def test_run_in_repository_thread_does_not_block_the_loop(tmp_path: pathlib.Path) -> None:
    """线程里的仓库调用在飞时，事件循环还在转。"""
    repository = _repository(tmp_path)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            ticks += 1
            await asyncio.sleep(0.001)

    task = asyncio.create_task(ticker())
    await run_in_repository_thread(
        repository.initialize_project,
        project_id="p", name="P", description=None, research_domain=None, owner_id="u",
    )
    await task
    assert ticks == 20
    assert module.run_in_repository_thread is run_in_repository_thread
