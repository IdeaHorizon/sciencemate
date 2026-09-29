"""`extract_material`：用户交来的归档只解到材料池，不进 Git；所有 producing 节点都拿得到。

2026-09-09 node20：observation 用 shell 把 64MB 的日志 tar 解到自己的节点目录，
4492 个文件被 checkpoint 全部收进会话分支，之后每一次"这个会话改了什么"都跟它
成正比，整台后端以 35 秒为量子停摆。解开的东西是材料的派生物，归属和材料一样。
"""
from __future__ import annotations

import io
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import materials
from core.tool_registry import get_tool
from shared.tools.library.materials_extract import EXTRACTED_SUFFIX, extract_material


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("# p\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _place(worktree: Path, name: str, payload: bytes) -> None:
    materials.place(worktree, name, io.BytesIO(payload), uploaded_by="u")


def _state(worktree: Path) -> SimpleNamespace:
    return SimpleNamespace(project_worktree=str(worktree), root=worktree / ".run")


@pytest.mark.asyncio
async def test_an_archive_lands_in_the_pool_directory_and_never_in_git(worktree: Path) -> None:
    _place(worktree, "logs.tar.gz", _tar_bytes({
        "asc25/log1.txt": b"a\n" * 10,
        "asc25/deep/log2.txt": b"b\n" * 10,
    }))
    result = await extract_material(state=_state(worktree), name="logs.tar.gz")
    assert result["status"] == "success", result
    assert result["files"] == 2
    target = worktree / result["path"]
    assert target == worktree / materials.MATERIALS_RELATIVE / f"logs.tar.gz{EXTRACTED_SUFFIX}"
    assert (target / "asc25" / "deep" / "log2.txt").read_bytes() == b"b\n" * 10
    # 与材料同一条 gitignore 兜着：git 看不见解开的任何东西
    untracked = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert "extracted" not in untracked, untracked
    assert not (target / "asc25" / "log1.txt").stat().st_mode & 0o222, "材料是输入，不是草稿"


@pytest.mark.asyncio
async def test_extraction_is_idempotent(worktree: Path) -> None:
    _place(worktree, "d.tar.gz", _tar_bytes({"x.txt": b"x"}))
    first = await extract_material(state=_state(worktree), name="d.tar.gz")
    second = await extract_material(state=_state(worktree), name="d.tar.gz")
    assert second["status"] == "success" and second["already_extracted"] is True
    assert second["path"] == first["path"]


@pytest.mark.asyncio
async def test_members_that_escape_the_directory_are_skipped(worktree: Path) -> None:
    _place(worktree, "evil.tar.gz", _tar_bytes({"../escape.txt": b"no", "ok.txt": b"ok"}))
    result = await extract_material(state=_state(worktree), name="evil.tar.gz")
    assert result["status"] == "success"
    assert result["files"] == 1 and result["skipped_members"] == 1
    assert not (worktree / materials.MATERIALS_RELATIVE / "escape.txt").exists()
    assert not (worktree / "escape.txt").exists()


@pytest.mark.asyncio
async def test_zip_archives_are_handled_too(worktree: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("a/b.csv", "1,2\n")
    _place(worktree, "data.zip", buffer.getvalue())
    result = await extract_material(state=_state(worktree), name="data.zip")
    assert result["status"] == "success" and result["files"] == 1


@pytest.mark.asyncio
async def test_unknown_materials_and_non_archives_are_refused_plainly(worktree: Path) -> None:
    _place(worktree, "table.csv", b"1,2\n")
    missing = await extract_material(state=_state(worktree), name="nope.tar.gz")
    assert missing["status"] == "error" and "table.csv" in missing["error"]
    plain = await extract_material(state=_state(worktree), name="table.csv")
    assert plain["status"] == "error" and "不是 tar/zip" in plain["error"]


def test_the_tool_is_registered_and_always_on_for_producing_nodes() -> None:
    from core.loader import _ALWAYS_ON_TOOLS, _with_always_on_tools

    assert get_tool("extract_material") is not None
    assert "extract_material" in _ALWAYS_ON_TOOLS
    assert "extract_material" in _with_always_on_tools(["read_file"], "observation")


def test_the_orchestrator_declares_it_too() -> None:
    """`_` 前缀的框架节点不吃 always-on 名单，调度器要自己声明（它也解过一份）。"""
    yaml_text = (Path(__file__).resolve().parents[1] / "nodes" / "_orchestrator" / "harness.yaml").read_text(
        encoding="utf-8"
    )
    assert "\n  - extract_material" in yaml_text
