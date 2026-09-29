"""import_artifact 只收 `sources/` 下的**外来件**（位置即来源）。

E2E v19 现场：writing 节点判 incomplete（`manuscript_pdf_compiled` 等四项没过），
experiment 也没能冻结 experiment_log。orchestrator 于是连调五次 import_artifact，
源路径全部指向别的节点自己的目录：

    experiment/artifacts/experiment_log__..._run_v2      （相对 + 绝对各一次）
    experiment/artifacts/clean_results__..._results_v2
    writing/artifacts/manuscript__...                    （相对 + 绝对各一次）

然后把抄出来的副本 freeze 掉。后果四条：

1. 权威副本跑到产出方之外 —— writing 自己那份 frozen=None，orchestrator 抄的
   那份 frozen=True。
2. 冻结的那份落在 `.research/orchestration/artifacts/`，**git untracked**；受
   版本控制的反倒是没冻的草稿。
3. provenance 被标成 `imported`，而这是流水线自己产的 —— 正是这个字段要防的失真。
4. 完成度闸被架空：节点冻不了本来就是因为 verdict/sediment 没落地、PDF 没过
   校验；抄一份出去冻上，闸等于不存在。（那份 experiment_log 副本的备注写着
   "三条假说全部 validated"，而 research_state v3 记的是 H2 inconclusive。）

模块 docstring 早写明了"外部材料"这个前提，只是从没有人验证它。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from core.ledger import workspace_store, write_record
from core.project_workspace import bind_project_workspace
from core.state import State
from shared.tools.library.artifact_intake import _import_artifact, _owning_node_of

NODES = ("literature", "hypothesis", "data", "experiment", "postprocess", "writing")

from core.project_workspace import _NODE_WORKSPACES as _DIRS  # 节点 → 目录


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "T"],
                 ["config", "user.email", "t@e.test"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "project.yaml").write_text("schema_version: 2\n", encoding="utf-8")
    (root / "sources").mkdir()
    (root / "reviews").mkdir()
    (root / "notes").mkdir()
    for node in NODES:
        (root / _DIRS[node]).mkdir(parents=True)
        (root / _DIRS[node] / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"],
                   check=True, capture_output=True)
    return root


def _orchestrator(tmp_path: Path, root: Path) -> State:
    (tmp_path / "runstate").mkdir(parents=True, exist_ok=True)
    state = State(run_id="orch", node_type="_orchestrator", root=tmp_path / "runstate")
    bind_project_workspace(state, root)
    return state


def _node_output(root: Path, node: str, artifact_type: str, name: str) -> Path:
    """某个 producing 节点自己的记录：节点目录里的原生文件 + 工作区账本一行。"""
    record = write_record(root, artifact_type=artifact_type, name=name, content="# Paper\n",
                          directory=_DIRS[node], produced_by_node_type=node,
                          produced_by_run_id=f"r-{node}")
    store = workspace_store(root)
    return store.abs_path(store.head(record["id"]))


# ── 认所有者 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("node", NODES)
def test_every_producing_node_directory_is_recognised(tmp_path: Path, node: str) -> None:
    """名单从 _NODE_WORKSPACES 现取，别在这道门里抄一份 —— 抄了就会漏新节点。"""
    root = _worktree(tmp_path)
    assert _owning_node_of(_orchestrator(tmp_path, root),
                           root / _DIRS[node] / "x.md") == node


def test_paths_outside_any_node_workspace_have_no_owner(tmp_path: Path) -> None:
    """所有权判定只认节点目录：sources/ 和工作区外都不归任何节点。"""
    root = _worktree(tmp_path)
    state = _orchestrator(tmp_path, root)

    assert _owning_node_of(state, root / "sources" / "someone_elses.pdf") is None
    assert _owning_node_of(state, root / "PROJECT.md") is None
    assert _owning_node_of(state, Path(tempfile.gettempdir()) / "upload.pdf") is None


@pytest.mark.asyncio
async def test_files_outside_sources_are_refused_with_directions(tmp_path: Path) -> None:
    """位置即来源：不在 sources/ 下的（哪怕也不属于任何节点）一律拒，
    且报错要说清该放哪 —— 别让模型去猜第二个落点。"""
    root = _worktree(tmp_path)
    stray = root / "PROJECT_NOTES_from_email.md"
    stray.write_text("外部合作者发来的记录", encoding="utf-8")

    result = await _import_artifact(
        state=_orchestrator(tmp_path, root), artifact_type="dataset",
        name="Notes", source_path=str(stray),
    )

    assert result["status"] == "error", result
    assert "sources/" in result["error"]
    assert result.get("expected_location") == "sources/"


# ── 整条链路回放 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_importing_another_nodes_output_is_refused(tmp_path: Path) -> None:
    """v19 真实调用：orchestrator 把 writing 的手稿当外来件导入。"""
    root = _worktree(tmp_path)
    source = _node_output(root, "writing", "manuscript", "Paper")
    assert source == root / "paper" / "manuscript__Paper.tex"

    result = await _import_artifact(
        state=_orchestrator(tmp_path, root), artifact_type="manuscript",
        name="Paper", source_path=str(source),
        note="Writing 节点产出，LaTeX 编译通过",
    )

    assert result["status"] == "error", result
    assert result["owning_node"] == "writing"
    # 报错必须给出正确做法，否则模型只会换个写法再试一次（v19 就是相对路径
    # 被拒后换绝对路径又试了一次）
    assert "read_artifact" in result["error"]


@pytest.mark.asyncio
async def test_the_relative_path_spelling_is_refused_too(tmp_path: Path) -> None:
    """v19 里同一份产物被用相对路径和绝对路径各导一次 —— 两种写法都得挡。"""
    root = _worktree(tmp_path)
    _node_output(root, "experiment", "clean_results", "R")
    state = _orchestrator(tmp_path, root)
    state.root = root          # 让相对路径按 worktree 解析，复刻当时的调用

    result = await _import_artifact(
        state=state, artifact_type="clean_results", name="R",
        source_path="experiments/clean_results__R.md",
    )

    assert result["status"] == "error", result
    assert result["owning_node"] == "experiment"


@pytest.mark.asyncio
async def test_a_genuinely_external_file_still_imports(tmp_path: Path) -> None:
    """守住入口本身：外部材料是这个工具存在的理由，不能连它一起拒了。"""
    root = _worktree(tmp_path)
    external = root / "sources" / "collaborator_dataset.json"
    external.write_text(json.dumps({"rows": [1, 2, 3]}), encoding="utf-8")

    result = await _import_artifact(
        state=_orchestrator(tmp_path, root), artifact_type="dataset",
        name="Collaborator", source_path=str(external),
        note="合作方提供，非本平台产出",
    )

    assert result["status"] == "success", result


def test_producing_nodes_still_cannot_import_at_all() -> None:
    """原有边界不能被这次改动削弱：只有 orchestrator 能导入。

    角色归注册表（判决拆除·第三波）：`ToolDefinition.allowed_node_types` 让
    producing 节点的工具面上根本没有这个工具，函数体不再查角色。
    """
    from core.bootstrap import bootstrap
    from core.tool_registry import get_tool, list_tools_for_node

    bootstrap()
    tool = get_tool("import_artifact")
    assert tool is not None
    assert tool.allowed_node_types == ["_orchestrator"]
    for node in ("writing", "experiment", "literature", "hypothesis"):
        names = {t.name for t in list_tools_for_node(node, ["import_artifact"])}
        assert "import_artifact" not in names, node
    assert {t.name for t in list_tools_for_node("_orchestrator", ["import_artifact"])} == {"import_artifact"}
