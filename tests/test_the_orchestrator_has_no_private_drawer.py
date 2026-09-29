"""调度器没有私人抽屉：作用域是用户看得见的 `notes/`，草稿落 run 目录，摆给用户的必须已入档。

2026-09-10 之前调度器的作用域是 `.research/orchestration/`。真项目里那儿躺着
`run_scan_E2.py`、`ising_results.json`、`test_ws.txt`，还有一次它把论文 PDF 编译在
那里，用户在文件树里找不到（整片被折成"平台记账"）。私人目录的问题不是脏，是它
成了交付物可以藏身的第三个地方，而且没人负责清。

规则和节点一样：位置由「这是什么」决定，不由「谁写的」决定。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import shared.tools.builtin  # noqa: F401  # register shared filesystem tools
from core import closing_manifest, project_bootstrap
from core.loop_hooks import HookContext
from core.loop_hooks_builtin import _presentation_gate_before_finish
from core.project_workspace import (
    _NODE_WORKSPACES,
    producing_node_dirs,
    system_node_dirs,
    working_directory,
)
from core.state import State
from core.tool_registry import execute


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text("schema_version: 2\nname: Test\n", encoding="utf-8")
    (root / ".gitignore").write_text(".research/runtime/\n", encoding="utf-8")
    for directory in (*producing_node_dirs(), *system_node_dirs()):
        (root / directory).mkdir(parents=True, exist_ok=True)
        (root / directory / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-q", "-m", "Initialize Project")
    return root


def _orchestrator(root: Path) -> State:
    return State.new(
        "_orchestrator", root / ".research" / "runtime" / "runs",
        project_id="p1", project_worktree=root,
    )


def test_the_orchestrator_owns_notes_and_nothing_under_research() -> None:
    assert _NODE_WORKSPACES["_orchestrator"] == "notes"
    assert _NODE_WORKSPACES["orchestrator"] == "notes"
    assert not any(v.startswith(".research") for v in _NODE_WORKSPACES.values()), (
        "`.research/` 是平台记账，不是任何节点的作用域"
    )
    # 建仓名单从同一张表推导，不再各抄一份。
    assert project_bootstrap._NODE_DIRS == producing_node_dirs()
    assert project_bootstrap._SYSTEM_DIRS == system_node_dirs() == ("reviews", "notes")


@pytest.mark.asyncio
async def test_relative_writes_land_in_scratch_not_in_the_record(tmp_path: Path) -> None:
    """它键入的相对路径落 run 目录 scratch/，git 对此一无所知。"""
    root = _worktree(tmp_path)
    state = _orchestrator(root)

    assert working_directory(state) == Path(state.root) / "scratch"
    result = await execute("write_file", state, path="test_ws.txt", content="scratch\n")

    assert result["status"] == "success"
    assert Path(result["path"]) == Path(state.root) / "scratch" / "test_ws.txt"
    assert not (root / "notes" / "test_ws.txt").exists()
    assert _git(root, "status", "--porcelain", "-uall") == ""


@pytest.mark.asyncio
async def test_named_record_writes_land_in_notes(tmp_path: Path) -> None:
    """要进记录的东西它得指名：`project/notes/<文件>`。这才是 git 看得见的。"""
    root = _worktree(tmp_path)
    state = _orchestrator(root)

    result = await execute(
        "write_file", state, path="project/notes/summary.md", content="# 结论\n",
    )

    assert result["status"] == "success"
    assert (root / "notes" / "summary.md").read_text(encoding="utf-8") == "# 结论\n"
    assert "notes/summary.md" in _git(root, "status", "--porcelain", "-uall")


@pytest.mark.asyncio
async def test_it_still_cannot_write_into_a_nodes_directory(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    state = _orchestrator(root)

    result = await execute(
        "write_file", state, path="project/hypothesis/plan.md", content="x",
    )

    assert result["status"] == "error", result
    assert "notes" in result["error"], result["error"]


def test_its_artifacts_are_visible_to_other_nodes(tmp_path: Path) -> None:
    """调度器的记录落 `notes/`（根下一层）+ 工作区账本：跨节点读看得见它的产物。

    此前它的产物在 `.research/orchestration/artifacts/`（两层深），
    跨节点的一层扫描一件都扫不到，只有目录页的 `**` 扫得到。
    """
    root = _worktree(tmp_path)
    orchestrator = _orchestrator(root)
    orchestrator.save_artifact("research_intent", "goal", "# 目标")

    reader = State.new("hypothesis", root / ".research" / "runtime" / "runs",
                       project_id="p1", project_worktree=root)
    listed = reader.list_artifacts("research_intent")
    assert [a["id"] for a in listed] == ["research_intent__goal"]
    assert listed[0]["owner_node"] == orchestrator.node_type == "_orchestrator", \
        "owner_node 答的是账本上的产出节点，不是目录名（notes）"


# ── 呈现即入档 ────────────────────────────────────────────────────────────

class _Message:
    def __init__(self, role: str, content: str) -> None:
        self.role, self.content = role, content


def _ctx(state: State, text: str) -> HookContext:
    return HookContext(state=state, harness=None, messages=[_Message("assistant", text)], turn=3)


def test_scratch_paths_in_the_outgoing_text_are_held_back(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    state = _orchestrator(root)
    scratch = ".research/runtime/runs/1788723286-9837f2/scratch/fig.png"

    held = _presentation_gate_before_finish(_ctx(state, f"结果如图：![收敛](" + scratch + ")"))

    assert held is not None and len(held) == 1
    assert scratch in held[0].content
    assert "project/notes/" in held[0].content, "递回去的话必须说清该放哪"
    events = [line for line in state.transcript_path.read_text().splitlines()
              if "presentation_gate_held" in line]
    assert events


def test_record_paths_pass_and_other_nodes_are_not_judged(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    state = _orchestrator(root)
    assert _presentation_gate_before_finish(_ctx(state, "见 [笔记](notes/summary.md) 和 ![图](notes/fig.png)")) is None
    # 只对锚在 scratch 的节点判：别的节点没有 scratch，正文也不直接给用户看。
    hypothesis = State.new("hypothesis", root / ".research" / "runtime" / "runs",
                           project_id="p1", project_worktree=root)
    assert _presentation_gate_before_finish(
        _ctx(hypothesis, "![x](.research/runtime/runs/r/scratch/x.png)")
    ) is None


def test_the_judgement_is_the_markdown_target_shape() -> None:
    """只认渲染层会当成文件处理的那一种形状 —— 提一嘴路径不算摆出来。"""
    text = "草稿在 .research/runtime/runs/r/scratch/a.png 里；![b](.research/runtime/runs/r/scratch/b.png)"
    assert closing_manifest.unpresentable_targets(text) == [".research/runtime/runs/r/scratch/b.png"]
    assert closing_manifest.unpresentable_targets("[论文](notes/latex_build/x/main.pdf)") == []
