"""Framework-owned Project workspace behavior; node packages stay unchanged."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import shared.tools.builtin  # noqa: F401  # register shared filesystem tools
import shared.tools.run_node  # noqa: F401  # register centralized dispatch tool
from core import sandbox
from core.harness import NodeHarness
from core.project_workspace import (
    request_completion_checkpoint,
    workspace_snapshot,
)
from core.state import State
from core.tool_registry import execute
from shared.tools.run_node import _finish_child

# 写时的墙现在是进程沙箱（core/sandbox.py）。跳过判据直接问机制自己
# （sandbox.availability），不在测试里重推一遍 —— 两处各推一遍就有两个会
# 各自演化的答案。见证那一半不受影响，任何部署下照跑。
from core.sandbox import availability

requires_sandbox = pytest.mark.skipif(
    not availability()[0], reason="这台机器起不了子进程写沙箱")


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text("schema_version: 2\nname: Test\n", encoding="utf-8")
    for directory in ("literature", "plan", "data", "experiments", "figures", "paper"):
        (root / directory).mkdir()
        (root / directory / "README.md").write_text(f"# {directory}\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "Initialize Project")
    return root


@pytest.mark.asyncio
async def test_shared_tool_writes_small_scripts_into_project_workspace(tmp_path: Path) -> None:
    project = _worktree(tmp_path)
    state = State.new(
        "literature",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )

    result = await execute(
        "write_file",
        state,
        path="scripts/collect_sources.py",
        content="print('collect')\n",
    )

    expected = project / "literature"
    assert result["status"] == "success"
    assert Path(result["path"]) == expected / "scripts/collect_sources.py"
    assert expected.joinpath("scripts/collect_sources.py").read_text() == "print('collect')\n"
    assert _git(project, "rev-parse", "HEAD") == _git(project, "rev-parse", "main")

    events = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    changed = [event for event in events if event.get("event") == "workspace_changed"]
    assert changed[-1]["tool_name"] == "write_file"
    assert changed[-1]["paths"] == ["literature/scripts/collect_sources.py"]


@pytest.mark.asyncio
async def test_node_reads_upstream_but_cannot_write_it(tmp_path: Path) -> None:
    project = _worktree(tmp_path)
    (project / "experiments/results.md").write_text("measured=7\n", encoding="utf-8")
    _git(project, "add", "experiments/results.md")
    _git(project, "commit", "-m", "experiment result")
    state = State.new(
        "writing",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )

    read = await execute("read_file", state, path="../experiments/results.md")
    denied = await execute(
        "write_file",
        state,
        path="../experiments/results.md",
        content="tampered\n",
    )

    assert read["status"] == "success" and "measured=7" in read["content"]
    assert denied["status"] == "error"
    assert "may only write paper" in denied["error"]
    assert (project / "experiments/results.md").read_text() == "measured=7\n"


@requires_sandbox
@pytest.mark.asyncio
async def test_shell_escape_fails_at_write_time(tmp_path: Path) -> None:
    """断言在 2026-08-11 翻转：从"写完了被回退"改成"根本写不进去"。

    2026-08-13 起"写不进去"由进程沙箱保证（core/sandbox.py）——只有模型的
    子进程被关进去，框架自己的写不受影响；chmod 版（进程无关、root 失效、
    还得豁免 .research/）整层删除。这条走 execute() 全链路：拒写 + 报错指到
    路径 + 翻译成"这是谁的、该怎么做"。
    """
    project = _worktree(tmp_path)
    state = State.new(
        "literature",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )

    try:
        result = await execute(
            "run_bash",
            state,
            cmd="printf tampered > ../plan/escape.txt",
        )

        assert result["status"] == "error"
        assert not (project / "plan/escape.txt").exists()
        # 原生报错要精确到那一行、那个路径（只读 bind/rootfs 报 EROFS/EPERM）。
        stderr = result.get("stderr_tail", "")
        assert (
            "Operation not permitted" in stderr
            or "Permission denied" in stderr
            or "Read-only file system" in stderr
        ), stderr
        assert "plan/escape.txt" in stderr
        # 且必须翻译成"这是谁的、该怎么做" —— 否则读起来像环境故障
        note = result.get("boundary_note") or ""
        assert "hypothesis" in note and "chmod" in note and "run_node" in note
    finally:
        if state.sandbox_manifest:
            sandbox.evict_state_attempt(state)


@pytest.mark.asyncio
async def test_guard_preserves_preexisting_parent_work_and_reverts_only_tampering(
    tmp_path: Path,
) -> None:
    """越界写只能被拒；沙盒不可用也不得执行到宿主文件系统。"""

    project = _worktree(tmp_path)
    parent_work = project / ".research/orchestration/plan.md"
    parent_work.parent.mkdir(parents=True, exist_ok=True)
    parent_work.write_text("legitimate parent draft\n", encoding="utf-8")
    state = State.new(
        "literature",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )

    try:
        own_write = await execute("write_file", state, path="notes.md", content="literature note\n")
        assert own_write["status"] == "success"
        assert parent_work.read_text() == "legitimate parent draft\n"

        tamper = await execute(
            "run_bash",
            state,
            cmd="printf corrupted > ../.research/orchestration/plan.md",
        )
        assert (project / "literature/notes.md").read_text() == "literature note\n"
        assert tamper["status"] == "error"
        assert parent_work.read_text() == "legitimate parent draft\n"
    finally:
        if state.sandbox_manifest:
            sandbox.evict_state_attempt(state)


@pytest.mark.asyncio
async def test_platform_checkpoint_between_tools_becomes_the_next_guard_baseline(
    tmp_path: Path,
) -> None:
    project = _worktree(tmp_path)
    state = State.new(
        "writing",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )
    upstream = project / "experiments/results.md"
    upstream.write_text("measured=11\n", encoding="utf-8")
    _git(project, "add", "experiments/results.md")
    _git(project, "commit", "-m", "Platform checkpoint")
    platform_head = _git(project, "rev-parse", "HEAD")

    result = await execute("read_file", state, path="../experiments/results.md")

    assert result["status"] == "success"
    assert "measured=11" in result["content"]
    assert _git(project, "rev-parse", "HEAD") == platform_head


@pytest.mark.asyncio
async def test_project_worktree_rejects_background_child_mutation_lane(tmp_path: Path) -> None:
    project = _worktree(tmp_path)
    state = State.new(
        "_orchestrator",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )
    state.hook_state["_callable_nodes"] = ["*"]

    result = await execute(
        "run_node",
        state,
        node_type="literature", user_note="测试派发",
        node_inputs={"task": "survey"},
        background=True,
    )

    assert result["status"] == "error"
    assert "Session worktree" in result["error"]
    assert "background" in result["error"]


def test_project_file_output_is_reviewable_without_artifact_import(tmp_path: Path) -> None:
    # v2.0：literature 已服务化（服务不登记 post-node flow）。本用例测的语义是
    # "写进 Project 工作区的文件也算可审查产出，不必导入成 artifact" —— 那是
    # producing 节点的语义，改用 hypothesis 举例。
    project = _worktree(tmp_path)
    state = State.new(
        "_orchestrator",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )
    harness = NodeHarness(node_type="hypothesis", system_prompt="")
    summary = {
        "run_id": "hypothesis-run",
        "node_type": "hypothesis",
        "status": "completed",
        "turns": 3,
        "state_dir": str(tmp_path / "child"),
        "artifacts": [],
        "project_workspace": {
            "workspace_prefix": "plan",
            "paths": ["plan/research-plan.md"],
        },
    }

    result = _finish_child(state, "hypothesis", {}, summary, harness, None)

    assert result["imported_artifacts"] == []
    assert result["project_workspace"]["paths"] == ["plan/research-plan.md"]
    assert result["can_start_standard_review"] is True
    # v2.1：可进标准 review 就必须**同时**登记 post-node flow —— #143 的
    # reviewer 门正是靠这条 entry 认人。两者不一致的后果实测过：v2 下
    # can_start_standard_review=True 但没有 entry，门只好整个排除 v2，于是
    # 不合格的 producer 也能拉起一轮 40 turn 的 review（E2E 烧 4.6M token）。
    flow = state.hook_state.get("pending_post_node_flow") or []
    assert len(flow) == 1
    assert flow[0]["producing_node"] == "hypothesis"
    assert flow[0]["review_state"] == "pending"


@pytest.mark.asyncio
async def test_generic_blocker_report_preserves_agent_judgment(tmp_path: Path) -> None:
    project = _worktree(tmp_path)
    state = State.new(
        "writing",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )

    result = await execute(
        "report_blocker",
        state,
        category="environment",
        summary="xelatex exited 127: executable not found",
        evidence_paths=["paper/build.log"],
        requested_action="Provide a usable TeX environment or authorize an alternative renderer.",
        suggested_owner="",
    )

    assert result["status"] == "success"
    assert result["blocker"]["category"] == "environment"
    assert result["blocker"]["suggested_owner"] == ""
    assert state.hook_state["blockers"][0]["retryable_after_change"] is True


@pytest.mark.asyncio
async def test_save_artifact_can_reference_a_managed_workspace_file(tmp_path: Path) -> None:
    project = _worktree(tmp_path)
    state = State.new(
        "writing",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )
    await execute(
        "write_file",
        state,
        path="draft.md",
        content="# Draft\n\nEvidence.\n",
    )

    saved = await execute(
        "save_artifact",
        state,
        artifact_type="manuscript",
        name="draft",
        content_from_file="draft.md",
    )

    assert saved["status"] == "success"
    assert state.read_artifact(saved["id"])["content"] == "# Draft\n\nEvidence.\n"


def test_node_completion_requests_one_central_checkpoint(tmp_path: Path) -> None:
    project = _worktree(tmp_path)
    state = State.new(
        "experiment",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )
    script = Path(state.workspace_root) / "simulate.py"
    script.write_text("print('run')\n", encoding="utf-8")

    snapshot = workspace_snapshot(state)
    checkpoint = request_completion_checkpoint(state, "completed")

    assert snapshot is not None
    assert checkpoint is not None
    assert checkpoint["paths"] == snapshot["paths"]
    events = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    requested = [
        event for event in events if event.get("event") == "workspace_checkpoint_requested"
    ]
    assert len(requested) == 1
    assert requested[0]["run_status"] == "completed"
    assert _git(project, "status", "--porcelain")


@pytest.mark.asyncio
async def test_saved_artifacts_land_in_the_node_git_directory_not_a_gitignored_cache(
    tmp_path: Path,
) -> None:
    """v2 的产物必须真的进 Git —— 下游靠"读上游节点目录"取料，这是协作模型的地基。

    E2E 实测事故（2026-08-07 真课题）：hypothesis 真产出了 pre_registration 等
    7 个 artifact，却在每一处都不可见 ——
      - Git 里没有（hypothesis/ 只有 README.md，git status 干净）
      - 平台 artifacts 表里也没有（v2 已不再走 stage_artifact_candidate）
    因为 save_artifact 一律落 run-local `<run>/artifacts/`，而 v2 的 run 目录在
    `.research/cache/`（**gitignored**）—— 产物躺在会被清理的缓存里。
    `write_file` 早就重定向到节点目录了，`save_artifact` 没有。
    """
    import subprocess

    project = _worktree(tmp_path)
    state = State.new(
        "hypothesis",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )
    saved = await execute(
        "save_artifact",
        state,
        artifact_type="pre_registration",
        name="LJ_Cooling_Prereg",
        content="# Pre-registration\n\nH1: 更快冷却 → 更高最终势能。\n",
    )
    assert saved["status"] == "success"

    files = sorted((project / "plan").glob("pre_registration__*"))
    assert files, "产物没落进节点自己的 Git 目录"
    path = files[0].resolve()
    # 必须在本节点自己的 Git 目录下，且不在 gitignored 缓存里
    assert "plan" in path.parts
    assert ".research" not in path.parts and "cache" not in path.parts
    # 而且 Git 真的看得见它（不被 .gitignore 吞掉）
    ignored = subprocess.run(
        ["git", "-C", str(project), "check-ignore", str(path)],
        capture_output=True, text=True,
    )
    assert ignored.returncode != 0, f"产物被 .gitignore 吞了：{path}"
    status = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True, text=True,
    ).stdout
    assert "plan/pre_registration__" in status, f"产物没出现在 git status：{status[:300]}"


def test_parent_guard_does_not_revert_a_dispatched_child_writing_its_own_dir(tmp_path: Path) -> None:
    """节点树：父派子、子写自己的目录是正规路径，不是越界。

    E2E 实测事故（2026-08-07）：产物改落节点 Git 目录后，hypothesis / experiment
    每次产出都被父的工作区守卫判成
    "Node '_orchestrator' may only write .research/orchestration" 并回滚
    （单次 revert 8-11 个文件），节点因此永远交不出东西。
    根因：守卫是**单节点视角**，而执行模型是**节点树**。此前子产物落在
    run-local .research/cache/（gitignored），git status 看不见，冲突一直潜伏。

    不变量没放松：子仍只能写自己的目录（子 state 的写入校验照旧），
    这里只是父在派发期间不再把它当外人。
    """
    from core.project_workspace import (
        _delegated_workspaces,
        enforce_after_tool,
        note_delegated_workspace,
    )
    from core.state import State

    project = _worktree(tmp_path)
    state = State.new(
        "_orchestrator", tmp_path / "runtime",
        project_id="project-1", project_worktree=project,
    )
    assert _delegated_workspaces(state) == []

    # 只登记**直接被派的那一个**。孙辈那次登记发生在子的 state 上（生产里
    # experiment 调 data 是 experiment 自己的 run_node），父收不到 —— 所以
    # 这里绝不能替父把 "data" 也登记一遍：那是在验证一个生产中不存在的场景。
    # 委派面必须由 note_delegated_workspace 自己按子树推导出来。
    note_delegated_workspace(state, "experiment")
    # 记录必须**活到 enforce**：守卫的比对发生在工具返回之后
    # （capture_before_tool → 工具体 → enforce_after_tool）。做成退出即清的
    # contextmanager 时，enforce 时记录已空，子节点的正当产出照样被回滚 ——
    # 实测踩过，而且当时单元测试还是绿的。
    assert {str(p) for p in _delegated_workspaces(state)} == {"experiments", "data"}
    # 由 enforce_after_tool 消费后清空：委派面的生命周期 = 这一次工具调用
    enforce_after_tool(state, "run_node")
    assert _delegated_workspaces(state) == []

    # 未绑 worktree（CLI）时是无操作，不影响既有行为
    plain = State.new("_orchestrator", tmp_path / "rt2", project_id="p2")
    note_delegated_workspace(plain, "hypothesis")
    assert _delegated_workspaces(plain) == []


@pytest.mark.asyncio
async def test_run_node_registers_delegation_at_the_tool_entry_point(tmp_path: Path) -> None:
    """委派登记必须埋在 run_node 的**工具入口** —— 那是所有派发路径的必经点。

    实测（2026-08-07 E2E v4/v5）：先把登记埋在 _execute_with_infra_retry 里，
    单元测试全绿，真实运行照旧回滚（run_node 有同步 / 后台 / 重试包装多条
    路径，埋在其中一条深处必漏）。第二版做成 contextmanager 退出即清，而守卫
    比对在工具**返回之后** —— 同样静默失效。
    两次都是"判据/生命周期挂错对象"，两次单元测试都没抓到。
    """
    from core.project_workspace import (
        _delegated_workspaces,
        capture_before_tool,
        enforce_after_tool,
    )
    from shared.tools.run_node import _run_node_tool

    project = _worktree(tmp_path)
    (project / "plan").mkdir(exist_ok=True)
    state = State.new(
        "_orchestrator", tmp_path / "runtime",
        project_id="project-1", project_worktree=project,
    )
    state.hook_state["_callable_nodes"] = ["*"]

    capture_before_tool(state)
    try:
        await _run_node_tool(state, node_type="hypothesis", user_note="测试派发", node_inputs={"research_question": "x"})
    except Exception:
        pass                      # 测试环境没有 LLM，子节点跑不起来是预期的
    # 委派面是**子树**：hypothesis 自己会调 literature（callable_nodes 声明），
    # 孙辈那次登记发生在子的 state 上，父收不到 —— 所以必须在这里就推导出来。
    assert [str(p) for p in _delegated_workspaces(state)] == ["plan", "literature"]

    # 子节点产物不得被父守卫回滚
    (project / "plan" / "artifacts").mkdir(parents=True, exist_ok=True)
    produced = project / "plan" / "artifacts" / "prereg.json"
    produced.write_text("{}", encoding="utf-8")
    # 孙辈（hypothesis→literature 文献服务）写自己的目录同样是正规路径。
    # 实测事故：2026-08-07 三次「派 hypothesis → 回滚 ['.git', 'literature']」，
    # 以及 2026-08-08「派 writing → 回滚 ['figures/artifacts/visual_brief__…']」
    # —— 后者让 orchestrator 得出「writing 画不了图」，改成自己先派 postprocess，
    # 论文配图的所有权从此错位。
    (project / "literature" / "artifacts").mkdir(parents=True, exist_ok=True)
    grandchild = project / "literature" / "artifacts" / "survey.json"
    grandchild.write_text("{}", encoding="utf-8")
    assert enforce_after_tool(state, "run_node") is None
    assert produced.exists(), "子节点写自己目录的产出被父守卫回滚了"
    assert grandchild.exists(), "孙节点（子调用的服务）写自己目录的产出被父守卫回滚了"


# （原 chmod 版"写时挡住"半边已删：进程内写属于框架信任面，沙箱只关模型的
# 子进程。同一不变量的真进程版见 test_out_of_bounds_writes_fail_at_write_time
# —— sibling 写在 spawn 的子进程里当场被拒。）
@pytest.mark.asyncio
async def test_subtree_relaxation_does_not_open_the_door_outside_a_dispatch(
    tmp_path: Path,
) -> None:
    """放宽只在**派发期间**成立：没派发时，越界照拦。

    否则"writing 能调 postprocess"就变成了"writing 随时能拿 shell 往
    postprocess/ 里写"。实测反例（2026-08-09）：experiment 用 `cp` 往
    ../writing/latex_build/ 拷 PDF —— 那是真越界，必须被指出来。

    墙在沙箱（子进程写别人目录当场 EPERM，见
    test_out_of_bounds_writes_fail_at_write_time）；这里验的是**见证**：
    降级部署下同样的越界要被报出来 —— 但只报告，不销毁（2026-08-13）。
    """
    from core.project_workspace import capture_before_tool, enforce_after_tool

    project = _worktree(tmp_path)
    state = State.new(
        "writing", tmp_path / "runtime",
        project_id="project-1", project_worktree=project,
    )
    # 沙箱的可写面按当前 state 现算：writing 没派发时不含 postprocess/
    from core import sandbox as _sandbox

    roots = {str(p) for p in _sandbox.write_roots_for(state)}
    assert str((project / "figures").resolve()) not in roots
    assert str((project / "paper").resolve()) in roots

    capture_before_tool(state)                      # 注意：没有 note_delegated_workspace

    smuggled = project / "figures" / "fig1.png"
    smuggled.write_bytes(b"png")                    # 模拟降级部署下的漏网写

    report = enforce_after_tool(state, "safe_run_bash")
    assert report is not None and "figures/fig1.png" in report["paths"], (
        "没在派发期间写别人的目录必须被报出来"
    )
    assert smuggled.exists(), "见证层不许动文件 —— 销毁能力已下线"


@pytest.mark.asyncio
async def test_writing_can_call_the_figure_service_without_the_parent_flagging_it(
    tmp_path: Path,
) -> None:
    """论文配图的所有权：writing 决定怎么画 → 调 postprocess → 三层都不报越界。

    2026-08-08 实测：orchestrator 派 writing，writing 按 `callable_nodes:
    [postprocess]` 调图表服务，服务写 `figures/artifacts/visual_brief__…`
    → 父守卫回滚 2 个路径、整轮报错。orchestrator 由此推出"writing 画不了图"，
    改走"自己先派 postprocess 出图、再派 writing 搬图"，全项目 65 次 postprocess
    里 61 次由调度器发起 —— 一条框架缺陷把节点职责改写了。

    销毁能力已于 2026-08-13 整体下线，所以这里验的是**见证不误报**：孙辈的
    正当产出不该被记到父头上。误报一样有复利代价 —— 上面那次改写职责，起点
    就是模型读到了一条说它越界的报错。
    """
    from core.project_workspace import (
        capture_before_tool,
        enforce_after_tool,
        note_delegated_workspace,
    )

    project = _worktree(tmp_path)

    # 父：orchestrator 派 writing（它只知道这一层）
    parent = State.new(
        "_orchestrator", tmp_path / "rt-parent",
        project_id="project-1", project_worktree=project,
    )
    capture_before_tool(parent)
    note_delegated_workspace(parent, "writing")

    # 子：writing 跑起来，写自己的稿子
    child = State.new(
        "writing", tmp_path / "rt-child",
        project_id="project-1", project_worktree=project,
    )
    capture_before_tool(child)
    (project / "paper" / "artifacts").mkdir(parents=True, exist_ok=True)
    (project / "paper" / "artifacts" / "manuscript.tex").write_text("x", encoding="utf-8")

    # 孙：writing 调图表服务，服务写自己的目录
    note_delegated_workspace(child, "postprocess")
    grandchild = State.new(
        "postprocess", tmp_path / "rt-grand",
        project_id="project-1", project_worktree=project,
    )
    capture_before_tool(grandchild)
    (project / "figures" / "artifacts").mkdir(parents=True, exist_ok=True)
    figure = project / "figures" / "fig1.png"
    figure.write_bytes(b"png")

    # 三层守卫依次收工，从里往外 —— 和真实返回顺序一致
    assert enforce_after_tool(grandchild, "save_artifact") is None
    assert enforce_after_tool(child, "run_node") is None
    assert enforce_after_tool(parent, "run_node") is None, (
        "父的见证把孙辈的正当产出报成了越界写"
    )
    assert figure.exists()


@pytest.mark.asyncio
async def test_artifact_display_path_is_anchored_not_counted(tmp_path: Path) -> None:
    """产物的相对路径按**真实锚点**算，不按固定层数猜。

    原来写死 self.root.parent.parent（假设 artifacts 永远在 run 目录下两层）。
    产物落进节点 Git 目录后这个假设不成立，relative_to 直接抛 ValueError，
    把 save_artifact / cluster_hypothesis_candidates / audit_* 一整批工具全
    打挂 —— 实测 hypothesis 因此报 environment blocker：
    "artifacts 目录路径无法 relative_to .research/cache/runtime"。
    """
    project = _worktree(tmp_path)
    (project / "plan").mkdir(exist_ok=True)
    state = State.new(
        "hypothesis", tmp_path / "runtime",
        project_id="project-1", project_worktree=project,
    )
    saved = await execute(
        "save_artifact", state,
        artifact_type="research_plan", name="Plan", content="# plan\n",
    )
    assert saved["status"] == "success"
    # v2：锚在 worktree 根 —— 正好是 Git 相对路径，对用户和 diff 都有意义
    assert saved["path"] == "plan/research_plan__Plan.md"

    # CLI（没绑 worktree）保持原行为
    plain = State.new("hypothesis", tmp_path / "rt2", project_id="p2")
    saved2 = await execute(
        "save_artifact", plain,
        artifact_type="research_plan", name="Plan", content="# plan\n",
    )
    assert saved2["status"] == "success"
    assert saved2["path"].endswith("artifacts/research_plan__Plan.md")
    assert not saved2["path"].startswith("plan/")


# ── 内联改动卡：一次工具调用改了什么，以及正文长什么样 ──────────────────
#
# 2026-08-17 用户原话：「这些 diff 它只显示增加了多少行，减去了多少行，
# 点开之后并不能看到这个 diff 的详细信息」。卡片的两半（数字与正文）必须
# 来自同一个口径：**这一次调用**，而不是整个节点目录对 HEAD 的累计。


def _changes(state: State) -> list[dict]:
    events = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    return [event for event in events if event.get("event") == "workspace_changed"]


@pytest.mark.asyncio
async def test_a_brand_new_file_carries_its_diff_body(tmp_path: Path) -> None:
    """截图里那一整批 `+N -0` 的文件全是新建的未跟踪文件。

    `git diff HEAD` 对它们输出为空 —— 旧实现因此手工补算行数，正文那边始终
    是空的：卡片能报 `+152`，却永远点不开。
    """
    project = _worktree(tmp_path)
    state = State.new(
        "writing", tmp_path / "runtime", project_id="project-1", project_worktree=project
    )

    await execute(
        "write_file", state, path="latex_build/report.tex", content="\\section{A}\nbody\n"
    )

    change = _changes(state)[-1]
    assert change["paths"] == ["paper/latex_build/report.tex"]
    assert change["file_stats"][0]["status"] == "added"
    assert "+\\section{A}" in change["patch"], "新建文件必须带正文，否则点开是空的"
    assert change["patch_truncated"] is False


@pytest.mark.asyncio
async def test_a_card_reports_only_this_call_not_the_whole_run(tmp_path: Path) -> None:
    """第二张卡不许把第一张卡的文件再数一遍。

    这正是截图里 "Edited 8 files +1122" 之后紧接着 "Edited 14 files +1869" 的
    成因：前 8 个文件对 HEAD 仍然是脏的，于是被整个又算了一遍。
    """
    project = _worktree(tmp_path)
    state = State.new(
        "writing", tmp_path / "runtime", project_id="project-1", project_worktree=project
    )

    await execute("write_file", state, path="first.md", content="one\n")
    await execute("write_file", state, path="second.md", content="two\n")

    first, second = _changes(state)[-2:]
    assert first["paths"] == ["paper/first.md"]
    assert second["paths"] == ["paper/second.md"], "上一次的文件不该再出现一遍"
    assert second["files_changed"] == 1
    assert second["additions"] == 1
    assert "first.md" not in second["patch"]


@pytest.mark.asyncio
async def test_an_unchanged_workspace_emits_nothing(tmp_path: Path) -> None:
    project = _worktree(tmp_path)
    state = State.new(
        "writing", tmp_path / "runtime", project_id="project-1", project_worktree=project
    )

    await execute("write_file", state, path="draft.md", content="same\n")
    before = len(_changes(state))
    await execute("read_file", state, path="draft.md")

    assert len(_changes(state)) == before, "什么都没改就不该有卡片"


def test_an_oversized_file_is_witnessed_without_copying_it_into_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一份每步都在长的模拟日志，不该每次工具调用都往对象库塞一份副本。

    只记身份（大小 + mtime）而不记内容 —— 它仍然被看见（继续增长会继续报），
    但正文如实说"未观测"，不假装展示了什么。
    """
    from core import project_workspace

    project = _worktree(tmp_path)
    state = State.new(
        "experiment", tmp_path / "runtime", project_id="project-1", project_worktree=project
    )
    monkeypatch.setattr(project_workspace, "_MAX_OBSERVED_BLOB_BYTES", 1_000)
    huge = project / "experiments/run.log"
    huge.write_text("x" * 4_000, encoding="utf-8")

    first = project_workspace.workspace_delta(state)
    huge.write_text("x" * 9_000, encoding="utf-8")
    second = project_workspace.workspace_delta(state)

    assert first is not None and "未观测内容" in first["patch"]
    assert second is not None, "文件继续长必须继续被看见"
    assert "9000" in second["patch"], "身份里得带上新的大小，否则增长看不见"
    assert "xxxxxxxx" not in second["patch"], "超限文件的内容不该被搬进 Git"


def test_the_checkpoint_still_sees_every_dirty_path(tmp_path: Path) -> None:
    """增量是给卡片看的；平台 checkpoint 要的仍是"全部脏路径"。

    两个口径写在两个函数里，别让其中一个悄悄改变另一个 —— 漏掉一条路径就是
    一份不会被提交的科研产物。
    """
    from core.project_workspace import workspace_delta

    project = _worktree(tmp_path)
    state = State.new(
        "writing", tmp_path / "runtime", project_id="project-1", project_worktree=project
    )
    (project / "paper/one.md").write_text("1\n", encoding="utf-8")
    workspace_delta(state)
    (project / "paper/two.md").write_text("2\n", encoding="utf-8")
    workspace_delta(state)

    checkpoint = request_completion_checkpoint(state, "completed")

    assert checkpoint is not None
    assert checkpoint["paths"] == ["paper/one.md", "paper/two.md"]


def test_a_huge_diff_is_cut_per_file_and_says_it_was_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """截断必须留下标记，且每个文件的头都得在 —— 砍掉头等于让文件从证据里消失。"""
    from core import project_workspace

    project = _worktree(tmp_path)
    state = State.new(
        "writing", tmp_path / "runtime", project_id="project-1", project_worktree=project
    )
    monkeypatch.setattr(project_workspace, "_MAX_FILE_PATCH_BYTES", 400)
    monkeypatch.setattr(project_workspace, "_MAX_PATCH_BYTES", 600)
    for name in ("a", "b"):
        (project / f"paper/{name}.txt").write_text(
            "".join(f"line {index}\n" for index in range(200)), encoding="utf-8"
        )

    delta = project_workspace.workspace_delta(state)

    assert delta is not None
    assert delta["patch_truncated"] is True
    assert delta["patch"].count("diff --git ") == 2, "每个文件都得留头"
    assert delta["additions"] == 400, "统计数是独立事实，不随正文截断缩水"
