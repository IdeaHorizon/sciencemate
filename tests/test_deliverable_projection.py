"""交付投影（run_node 的 deliverable 契约，2026-08-31）。

病根：产物的正式身份（artifact 账本）与用户拿到手的呈现（他点名的文件）是
两个东西，此前平台只有前者——literature 落一个 survey_report__*.json，
用户点名的 LITERATURE_REVIEW.md 从头到尾不出现；调度器手工解 JSON 搬 content
靠模型自觉，不配叫机制。

修法：run_node(deliverable={artifact_type, path}) 把契约带进子 run，
save_artifact 落账（必经之路）时**框架**把 content 投影到用户点名的路径。
模型不搬字节；判据（validate_deliverable_projection）派发端与落盘端同一份。
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import shared.tools.builtin  # noqa: F401  # register shared filesystem tools
from core.project_workspace import (
    ProjectWorkspaceError,
    validate_deliverable_projection,
)
from core.state import State
from core.tool_registry import execute


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
    for directory in ("literature", "hypothesis", "data", "experiment", "postprocess", "writing"):
        (root / directory).mkdir()
        (root / directory / "README.md").write_text(f"# {directory}\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "Initialize Project")
    return root


def _literature(tmp_path: Path, contract: dict | None) -> tuple[State, Path]:
    project = _worktree(tmp_path)
    state = State.new("literature", tmp_path / "runtime",
                      project_id="project-1", project_worktree=project)
    if contract is not None:
        state.hook_state["_deliverable_projection"] = contract
    return state, project


# ── 判据（两端同一份）─────────────────────────────────────────────────────

def test_validator_accepts_root_and_own_scope(tmp_path: Path) -> None:
    project = _worktree(tmp_path)
    assert validate_deliverable_projection(
        project, "literature", "LITERATURE_REVIEW.md") == "LITERATURE_REVIEW.md"
    assert validate_deliverable_projection(
        project, "literature", "literature/REVIEW.md") == "literature/REVIEW.md"


@pytest.mark.parametrize("bad", [
    "paper/REVIEW.md",              # 别的节点的作用域
    "artifacts/x.json",               # 框架状态签名
    "literature/artifacts/x.json",    # 同上（节点内的 artifacts 命名空间）
    ".research/releases/x.md",        # 框架状态
    "project.yaml",                   # 平台管理的配置
    "../escape.md",                   # 越界
    "/tmp/abs.md",                    # 绝对路径
    "",                               # 空
])
def test_validator_rejects_everything_owned(tmp_path: Path, bad: str) -> None:
    project = _worktree(tmp_path)
    with pytest.raises(ProjectWorkspaceError):
        validate_deliverable_projection(project, "literature", bad)


# ── 落盘端：save_artifact 的必经之路上投影 ────────────────────────────────

@pytest.mark.asyncio
async def test_framework_projects_the_named_file(tmp_path: Path) -> None:
    state, project = _literature(
        tmp_path, {"artifact_type": "survey_report", "path": "LITERATURE_REVIEW.md"})
    out = await execute("save_artifact", state, artifact_type="survey_report",
                        name="wmles", content="# 综述\n\n结论。\n")
    assert out["status"] == "success", out
    assert out.get("deliverable_projected_to") == "LITERATURE_REVIEW.md"
    assert (project / "LITERATURE_REVIEW.md").read_text() == "# 综述\n\n结论。\n"


@pytest.mark.asyncio
async def test_resave_updates_the_projection(tmp_path: Path) -> None:
    state, project = _literature(
        tmp_path, {"artifact_type": "survey_report", "path": "LITERATURE_REVIEW.md"})
    await execute("save_artifact", state, artifact_type="survey_report",
                  name="wmles", content="v1\n")
    await execute("save_artifact", state, artifact_type="survey_report",
                  name="wmles", content="v2\n")
    assert (project / "LITERATURE_REVIEW.md").read_text() == "v2\n"


@pytest.mark.asyncio
async def test_other_types_do_not_project(tmp_path: Path) -> None:
    state, project = _literature(
        tmp_path, {"artifact_type": "survey_report", "path": "LITERATURE_REVIEW.md"})
    out = await execute("save_artifact", state, artifact_type="literature_index",
                        name="idx", content="{}")
    assert out["status"] == "success"
    assert "deliverable_projected_to" not in out
    assert not (project / "LITERATURE_REVIEW.md").exists()


@pytest.mark.asyncio
async def test_no_contract_means_no_projection(tmp_path: Path) -> None:
    state, project = _literature(tmp_path, None)
    out = await execute("save_artifact", state, artifact_type="survey_report",
                        name="wmles", content="# 综述\n")
    assert out["status"] == "success"
    assert not (project / "LITERATURE_REVIEW.md").exists()


@pytest.mark.asyncio
async def test_projection_failure_is_loud_not_silent(tmp_path: Path) -> None:
    """契约在派发端验过，但落盘端还要守一遍（两端可能隔了一次重派）。
    投影失败不牺牲账——success 照回，失败原因在结果里吵出来。"""
    state, project = _literature(
        tmp_path, {"artifact_type": "survey_report", "path": "paper/REVIEW.md"})
    out = await execute("save_artifact", state, artifact_type="survey_report",
                        name="wmles", content="# 综述\n")
    assert out["status"] == "success"
    assert "deliverable_projection_error" in out
    assert not (project / "paper/REVIEW.md").exists()


# ── 接线：run_node 真把契约递给了 executor ────────────────────────────────

def test_run_node_wires_the_contract_through() -> None:
    """查名字出现≠查接线：AST 比对签名与实参——
    _run_node_tool 收 deliverable、exec_kwargs 带 deliverable、
    execute_node 收 deliverable 并写 hook_state。"""
    rn = ast.parse(Path("shared/tools/run_node.py").read_text())
    fn = next(n for n in ast.walk(rn) if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "_run_node_tool")
    assert "deliverable" in [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "exec_kwargs" for t in n.targets))
    assert "deliverable" in [k.arg for k in call.value.keywords]

    ex = ast.parse(Path("core/executor.py").read_text())
    en = next(n for n in ast.walk(ex) if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "execute_node")
    assert "deliverable" in [a.arg for a in en.args.args + en.args.kwonlyargs]
    src = Path("core/executor.py").read_text()
    assert '_deliverable_projection"] = dict(deliverable)' in src
