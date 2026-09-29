"""P6-a：list_files / search_files —— artifact CRUD 的通用替身。

删 list_artifacts / search_artifacts 的前置：不是每个节点都有 bash，没有
枚举与检索能力就删不了专用工具。边界与 read_file 完全一致：读全 worktree
（读跨节点、写不跨节点）、输出有界、敏感路径拒绝、报错列出正确答案。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.project_workspace import bind_project_workspace
from core.state import State
from shared.tools.builtin import _list_files, _search_files

NODES = ("literature", "hypothesis", "experiment", "writing")

from core.project_workspace import _NODE_WORKSPACES as _DIRS  # 节点 → 目录


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "T"],
                 ["config", "user.email", "t@e.test"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    for node in NODES:
        d = root / _DIRS[node] / "artifacts"
        d.mkdir(parents=True)
        (d / f"{node}_note.md").write_text(f"# {node}\nTg = 0.44\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"],
                   check=True, capture_output=True)
    return root


def _state(tmp_path: Path, root: Path, node_type: str = "writing") -> State:
    state = State.new(node_type=node_type, base_dir=tmp_path / "runs", project_id="p6a")
    bind_project_workspace(state, root)
    return state


# ── list_files ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lists_a_directory(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    result = await _list_files(_state(tmp_path, root), path=str(root / "experiments" / "artifacts"))
    assert result["status"] == "success", result
    names = [e["path"] for e in result["entries"]]
    assert "experiment_note.md" in names


@pytest.mark.asyncio
async def test_writing_can_list_another_nodes_directory(tmp_path: Path) -> None:
    """读跨节点 —— 这是取代 list_artifacts 的关键能力。"""
    root = _worktree(tmp_path)
    result = await _list_files(_state(tmp_path, root, "writing"),
                               path=str(root), glob="*/artifacts/*.md")
    assert result["status"] == "success", result
    found = {e["path"] for e in result["entries"]}
    assert "experiments/artifacts/experiment_note.md" in found
    assert "plan/artifacts/hypothesis_note.md" in found


@pytest.mark.asyncio
async def test_listing_is_bounded_and_says_so(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    bulk = root / "paper" / "artifacts"
    for i in range(30):
        (bulk / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
    result = await _list_files(_state(tmp_path, root), path=str(bulk), max_entries=5)
    assert len(result["entries"]) == 5
    assert result["truncated"] is True, "列到上限却没说 —— 模型会把 5 条当成全部"


@pytest.mark.asyncio
async def test_missing_directory_error_lists_real_neighbours(tmp_path: Path) -> None:
    """报错必须列出正确答案（v18 连猜 5 次的教训）。"""
    root = _worktree(tmp_path)
    # 目录按研究语义命名之后，节点名 `experiment/` 不再是目录 —— 正是模型会猜错的写法。
    result = await _list_files(_state(tmp_path, root), path=str(root / "experiment"))
    assert result["status"] == "error"
    assert "experiments" in result["error"], result["error"]


@pytest.mark.asyncio
async def test_git_internals_are_pruned(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    result = await _list_files(_state(tmp_path, root), path=str(root), glob="**/*")
    assert all(".git" not in e["path"] for e in result["entries"])


# ── search_files ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_finds_lines_across_nodes(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    result = await _search_files(_state(tmp_path, root, "writing"),
                                 pattern=r"Tg = 0\.\d+", path=str(root))
    assert result["status"] == "success", result
    files = {m["file"] for m in result["matches"]}
    assert "experiments/artifacts/experiment_note.md" in files
    assert all(m["line"] == 2 for m in result["matches"])


@pytest.mark.asyncio
async def test_search_is_bounded_and_says_so(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    big = root / "paper" / "artifacts" / "big.log"
    big.write_text("hit\n" * 500, encoding="utf-8")
    result = await _search_files(_state(tmp_path, root), pattern="hit",
                                 path=str(big), max_matches=10)
    assert len(result["matches"]) == 10
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_invalid_regex_is_a_clear_error(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    result = await _search_files(_state(tmp_path, root), pattern="([", path=str(root))
    assert result["status"] == "error"
    assert "正则" in result["error"]


@pytest.mark.asyncio
async def test_search_respects_glob_filter(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    (root / "paper" / "artifacts" / "data.csv").write_text("Tg = 0.99\n", encoding="utf-8")
    result = await _search_files(_state(tmp_path, root), pattern="Tg",
                                 path=str(root), glob="*/artifacts/*.csv")
    files = {m["file"] for m in result["matches"]}
    assert files == {"paper/artifacts/data.csv"}, files
