"""交付写权（harness.deliverable_writes，2026-08-31）。

背景：orchestrator 此前没有任何写文件工具，"用户要一份 md"也只能起完整
producing 节点——实测一次 9 文件的文献梳理走了 26 分钟 / 82 次工具调用，
产物还落在 literature/artifacts/*.json 里而不是用户点名的文件。

放开的只是**无主之地**（项目根的普通文件）。三道闸并立，每道各测：
  1. producing 节点的目录 / MEMORY.md —— 写边界照旧拦，报错指路属主
  2. artifacts/ / project.yaml 等框架状态 —— _PROTECTED_SIG 硬拒
     （与 shell/python 同一份签名：同一条不变量的第三条到达路径）
  3. 其余 .research/* —— 框架状态，交付写权不涉足

全部用例走真入口 execute()——判据不落在"函数返回什么"，落在"盘上有没有那个
文件"（教训：断言只落 verdict 上分不清走的是哪条路）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import shared.tools.builtin  # noqa: F401  # register shared filesystem tools
from core.state import State
from core.tool_registry import execute
from shared.lib import dangerous_commands as dc


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text("schema_version: 2\nname: Test\n", encoding="utf-8")
    (root / "MEMORY.md").write_text("# memory\n", encoding="utf-8")
    for directory in ("literature", "hypothesis", "data", "experiment", "postprocess", "writing"):
        (root / directory).mkdir()
        (root / directory / "README.md").write_text(f"# {directory}\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "Initialize Project")
    return root


def _orchestrator(tmp_path: Path, *, flag: bool = True) -> tuple[State, Path]:
    project = _worktree(tmp_path)
    state = State.new(
        "_orchestrator",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )
    state.hook_state["_deliverable_writes"] = flag
    return state, project


# ── 放行：无主之地 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_named_deliverable_at_project_root(tmp_path: Path) -> None:
    state, project = _orchestrator(tmp_path)
    out = await execute("write_file", state,
                        path="project/LITERATURE_REVIEW.md", content="# 综述\n")
    assert out["status"] == "success", out
    assert (project / "LITERATURE_REVIEW.md").read_text() == "# 综述\n"


@pytest.mark.asyncio
async def test_edit_follows_the_same_scope(tmp_path: Path) -> None:
    state, project = _orchestrator(tmp_path)
    await execute("write_file", state,
                  path="project/NOTES.md", content="draft one\n")
    out = await execute("edit_file", state, path="project/NOTES.md",
                        old_string="one", new_string="two")
    assert out["status"] == "success", out
    assert (project / "NOTES.md").read_text() == "draft two\n"


# ── 第 1 道：属主地盘照旧拦，报错指路 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_producing_node_scope_still_owned(tmp_path: Path) -> None:
    state, project = _orchestrator(tmp_path)
    out = await execute("write_file", state,
                        path="project/literature/notes.md", content="x\n")
    assert out["status"] == "error"
    assert "literature" in out["error"] and "run_node" in out["error"]
    assert not (project / "literature/notes.md").exists()


@pytest.mark.asyncio
async def test_memory_still_owned_by_curator(tmp_path: Path) -> None:
    state, project = _orchestrator(tmp_path)
    out = await execute("write_file", state,
                        path="project/MEMORY.md", content="scribble\n")
    assert out["status"] == "error"
    assert (project / "MEMORY.md").read_text() == "# memory\n"


# ── 第 2 道：框架状态签名（与 shell/python 同一份）────────────────────────

@pytest.mark.asyncio
async def test_artifact_namespace_hard_denied(tmp_path: Path) -> None:
    state, project = _orchestrator(tmp_path)
    out = await execute("write_file", state,
                        path="project/artifacts/survey__x.json", content="{}")
    assert out["status"] == "error"
    assert "save_artifact" in out["error"]
    assert not (project / "artifacts").exists()


@pytest.mark.asyncio
async def test_project_yaml_hard_denied(tmp_path: Path) -> None:
    state, project = _orchestrator(tmp_path)
    out = await execute("write_file", state,
                        path="project/project.yaml", content="name: Hijacked\n")
    assert out["status"] == "error"
    assert "name: Test" in (project / "project.yaml").read_text()


# ── 第 3 道：其余 .research/* 是框架状态 ──────────────────────────────────

@pytest.mark.asyncio
async def test_research_state_outside_own_scope_denied(tmp_path: Path) -> None:
    state, project = _orchestrator(tmp_path)
    out = await execute("write_file", state,
                        path="project/.research/releases/x.md", content="x\n")
    assert out["status"] == "error"
    assert not (project / ".research/releases/x.md").exists()


# ── flag 关掉 = 今日行为一字不变 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_without_flag_root_write_still_denied(tmp_path: Path) -> None:
    state, project = _orchestrator(tmp_path, flag=False)
    out = await execute("write_file", state,
                        path="project/LITERATURE_REVIEW.md", content="# 综述\n")
    assert out["status"] == "error"
    assert not (project / "LITERATURE_REVIEW.md").exists()


@pytest.mark.asyncio
async def test_producing_node_behaviour_unchanged(tmp_path: Path) -> None:
    """literature（无 flag）写自己作用域照旧成功——收缩要有边界。"""
    project = _worktree(tmp_path)
    state = State.new("literature", tmp_path / "runtime",
                      project_id="project-1", project_worktree=project)
    out = await execute("write_file", state, path="notes.md", content="ok\n")
    assert out["status"] == "success", out
    assert (project / "literature/notes.md").read_text() == "ok\n"


# ── 契约：orchestrator 的 harness 真的声明了这套 ──────────────────────────

def test_orchestrator_harness_declares_the_capability() -> None:
    """文案许诺的能力接口得给得出：flag 与工具必须同时在场——
    只给 flag 不给工具（或反过来）都是那种"广告与默认不一致"的断线。"""
    from core.loader import load_harness

    harness = load_harness("_orchestrator")
    assert harness.deliverable_writes is True
    assert "write_file" in harness.tools
    assert "edit_file" in harness.tools


def test_protected_path_matcher_shares_the_signature() -> None:
    assert dc.match_protected_file_path("artifacts/x.json")
    assert dc.match_protected_file_path("literature/artifacts/x.json")
    assert dc.match_protected_file_path("project.yaml")
    assert dc.match_protected_file_path("kb_claims.jsonl")
    assert dc.match_protected_file_path("LITERATURE_REVIEW.md") is None
    assert dc.match_protected_file_path("docs/summary_of_findings.md") is None
