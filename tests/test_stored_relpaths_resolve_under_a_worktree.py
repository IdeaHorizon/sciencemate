"""写进记录里的相对路径，读方必须解回同一个文件。

背景（2026-08-27，qinp 配图闸卡死 4/4）：`e52cace5` 把节点产物的锚点从 run 根搬到
Project worktree，只改了**写方**（`paths.display_relpath`）。读方原地留着
`state.root / rel`，于是：

    写： <worktree>/postprocess/figures/fig.png                            ← 真的在
    读： <worktree>/.research/cache/runtime/runs/<id>/postprocess/figures/fig.png  ← 从来没有过

同一个字符串两端指向两个地方，两边都不报错，症状是"文件不存在" —— 与图真的没渲
出来完全无法区分。平台上配图闸从那天起 18 天一次没通过过，而 CI 全绿：
postprocess 的 21 个测试文件**没有一个绑 Project worktree**，不绑时写读恰好同锚。

所以这份测试的第一条纪律是：**必须绑真 worktree，且 run 根必须落在 worktree 里的
缓存目录下**（平台的真实形状）。写"看起来在测锚点"但两个锚点恰好重合的测试，等于
没测。`test_the_two_anchors_really_diverge` 就是防这一条的。
"""
from __future__ import annotations

import ast
import inspect
import subprocess
from pathlib import Path

import pytest

from core import paths
from core.project_workspace import bind_project_workspace
from core.state import State

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bound_state(tmp_path: Path, node_type: str = "postprocess") -> State:
    """平台的真实形状：绑 worktree，run 根在 `<worktree>/.research/cache/…` 下。"""
    wt = tmp_path / "wt"
    wt.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"],
                ["git", "commit", "-qm", "init", "--allow-empty"]):
        subprocess.run(cmd, cwd=wt, check=True)
    run_root = wt / ".research" / "cache" / "runtime" / "runs" / "run-1"
    run_root.mkdir(parents=True, exist_ok=True)
    state = State(run_id="run-1", node_type=node_type, root=run_root)
    bind_project_workspace(state, wt)
    return state


@pytest.fixture
def bound(tmp_path: Path) -> State:
    return _bound_state(tmp_path)


# ── 0. 这份测试自己有没有测到东西 ────────────────────────────────────────
def test_the_two_anchors_really_diverge(bound: State) -> None:
    """两个锚点必须是不同的地方，否则下面每一条都是空转。

    这条是给未来的：哪天 run 根搬回节点目录下，下面的用例会在"两端碰巧同答案"
    的情况下继续全绿，而真正的缺陷可以原样活着（`feedback_right_verdict_wrong_path`）。
    """
    anchors = paths.display_anchors(bound)
    assert len(anchors) == 2, anchors
    assert anchors[0] == Path(bound.project_worktree).resolve()
    assert anchors[1] == bound.root.resolve()
    assert anchors[0] != anchors[1]
    assert bound.root.is_relative_to(anchors[0]), "run 根应当嵌在 worktree 里（平台形状）"


# ── 1. 写方与读方成对（扫 core.paths，不写名单）──────────────────────────
def _state_taking_dir_helpers() -> list:
    """`core.paths` 里所有"给一个 State、返回一个目录"的具名 helper。

    扫模块而不是列名单：明天有人加了 `foo_dir()`，这条对称性自动覆盖它；
    写名单的话新 helper 默认漏过（`feedback_guardrails_must_scan_not_list`）。
    """
    found = []
    for name, fn in vars(paths).items():
        if not (name.endswith("_dir") and callable(fn) and not name.startswith("_")):
            continue
        params = list(inspect.signature(fn).parameters.values())
        if not params or params[0].name != "state":
            continue
        # 只要"给个 State 就能算出目录"的那些；`node_output_dir` 这类还要 node_type
        # 的通用漏斗由它自己的用例覆盖。
        if any(p.default is inspect.Parameter.empty for p in params[1:]
               if p.kind is not inspect.Parameter.VAR_KEYWORD):
            continue
        found.append((name, fn))
    return sorted(found)


def test_scan_found_the_helpers() -> None:
    names = [n for n, _ in _state_taking_dir_helpers()]
    assert "postprocess_figures_dir" in names
    assert "writing_bundles_dir" in names
    assert len(names) >= 6, names


@pytest.mark.parametrize("name", [n for n, _ in _state_taking_dir_helpers()])
def test_display_relpath_round_trips_for_every_node_output_dir(
    tmp_path: Path, name: str
) -> None:
    """`resolve_display_relpath(display_relpath(p)) == p`，对每个产物目录都成立。"""
    fn = dict(_state_taking_dir_helpers())[name]
    owner = name.split("_", 1)[0]
    state = _bound_state(tmp_path, node_type=owner if owner != "latex" else "writing")

    takes_create = "create" in inspect.signature(fn).parameters
    directory = fn(state, create=True) if takes_create else fn(state)
    directory.mkdir(parents=True, exist_ok=True)
    written = directory / "probe.bin"
    written.write_bytes(b"x")

    rel = paths.display_relpath(state, written)
    assert not Path(rel).is_absolute(), f"{name} 的产物路径没能相对化：{rel}"
    assert paths.resolve_display_relpath(state, rel) == written.resolve(), (
        f"{name}：写 {written} / 读 {paths.resolve_display_relpath(state, rel)}"
    )


def test_naive_join_is_what_used_to_break(bound: State) -> None:
    """把根因钉死：老写法在同一个输入上必然指向不存在的位置。

    这条不是在测产品代码，是在证明上一条真的跨过了锚点分叉 —— 否则"读对了"
    可能只是因为两个锚点碰巧同一个地方。
    """
    figures = paths.postprocess_figures_dir(bound, create=True)
    written = figures / "fig.png"
    written.write_bytes(b"\x89PNG\r\n\x1a\n")
    rel = paths.display_relpath(bound, written)

    assert (bound.root / rel).exists() is False, "老写法竟然找得到 —— 锚点没分叉，这份测试在空转"
    assert paths.resolve_display_relpath(bound, rel) == written.resolve()


def test_missing_file_names_every_anchor_it_tried(bound: State) -> None:
    """一个都没命中时，错误信息必须说清楚找过哪儿。

    锚点分叉和"文件真的没产出来"症状一模一样。不把候选位置打出来，下一次
    这类分叉又要靠人从头查（这次查了 18 天）。
    """
    tried = paths.display_relpath_candidates(bound, "figures/figures/nope.png")
    assert len(tried) == 2
    assert tried[0] == (Path(bound.project_worktree) / "figures/figures/nope.png").resolve()
    assert paths.resolve_display_relpath(bound, "figures/figures/nope.png") == tried[0], (
        "全落空时应指向首选锚点，好让报错指向它本该在的地方"
    )


# ── 2. 真正卡住 qinp 的那条路：figure 记录的读写锚点成对 ─────────────────
#
# B 刀之后 render/validation 的 DSL 面已死；锚点不变量原样落在新路径上：
# render_figure 把输出文件记成 display relpath，VLM/消费端必须在**同一个**
# 锚点把它解回来（写读锚点分叉 = 配图闸 18 天全红的根因，PR#707）。


def test_vlm_review_finds_the_png(bound: State) -> None:
    """机械审计之后 inspect/witness 要自己再取一次 PNG —— 同一锚点。"""
    from nodes.postprocess.tools.figure import _preferred_png

    written = paths.postprocess_figures_dir(bound, create=True) / "review.png"
    written.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    record = {"metadata": {"files": [
        {"format": "png", "path": paths.display_relpath(bound, written)}
    ]}}

    assert _preferred_png(bound, record) == written.resolve()


def test_figure_file_outside_the_run_tree_is_still_rejected(bound: State, tmp_path: Path) -> None:
    """修锚点不等于拆边界：树外的文件照旧拒。"""
    from nodes.postprocess.contracts import VisualContractError
    from nodes.postprocess.tools.figure import _preferred_png

    outside = tmp_path / "elsewhere" / "fig.png"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"x")
    record = {"metadata": {"files": [{"format": "png", "path": str(outside)}]}}
    with pytest.raises(VisualContractError):
        _preferred_png(bound, record)


# ── 3. 跨节点转发同样是"存下来的相对路径" ────────────────────────────────
def test_forwarded_file_is_found_at_the_worktree_anchor(tmp_path: Path) -> None:
    from core.executor import _materialize_forwarded_file

    parent = _bound_state(tmp_path, node_type="postprocess")
    child_root = Path(parent.project_worktree) / ".research/cache/runtime/runs/child"
    child_root.mkdir(parents=True, exist_ok=True)
    child = State(run_id="child", node_type="writing", root=child_root)
    bind_project_workspace(child, Path(parent.project_worktree))

    source = paths.postprocess_figures_dir(parent, create=True) / "fig.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n")
    metadata = {"file_path": paths.display_relpath(parent, source)}

    updated = _materialize_forwarded_file(
        child_state=child, parent_state=parent,
        artifact={"type": "figure", "name": "fig"}, metadata=metadata,
    )

    assert updated["file_path"] != metadata["file_path"], "没搬过去 —— 源文件没找到"
    landed = paths.resolve_display_relpath(child, updated["file_path"])
    assert landed.exists() and landed.read_bytes().startswith(b"\x89PNG")


# ── 4. 护栏：再有人手拼 run 根就转红（扫盘，不写名单）────────────────────
#: 允许保留的 `state.root / <非字面量>`。每条都必须**查过写方**才能进来 ——
#: 光看形状会误判（我第一版就把 data 的 `_delivery_path` 错判成拼错了，
#: 实际它解的 package 按设计就锚在 run 根：core/paths.py 的 LEGACY_IN_PLACE 段）。
_RUN_LOCAL_BY_DESIGN = {
    ("nodes/data/data_agent_loop.py",
     "_delivery_path 解的是 preprocessing package；写方（package_publisher / "
     "execute_preprocessing_plan / atomic_structure_recovery）全部锚在 run 根 "
     "(core.paths.data_workspace_dir，LEGACY_IN_PLACE)，读写同锚，一致"),
}


def _production_files() -> list[Path]:
    for base in ("core", "shared", "nodes"):
        for f in sorted((REPO_ROOT / base).rglob("*.py")):
            rel = f.relative_to(REPO_ROOT).as_posix()
            if "__pycache__" in rel or "/tests/" in rel or Path(rel).name.startswith("test_"):
                continue
            yield f


def _leftmost(node: ast.AST) -> ast.AST:
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        node = node.left
    return node


def _state_root_joins(tree: ast.AST) -> list[ast.BinOp]:
    """`<某个 state>.root / <非字面量>` 的拼接。

    右边是字面量（`state.root / "planning"`）不算：那是本 run 自己的目录，
    与"把别人存下来的相对路径解回文件"是两件事。护栏只盯后者。
    """
    hits = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)):
            continue
        lm = _leftmost(n)
        if not (isinstance(lm, ast.Attribute) and lm.attr == "root"):
            continue
        base = lm.value
        base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if "state" not in base_name:
            continue
        innermost = n
        while isinstance(innermost.left, ast.BinOp):
            innermost = innermost.left
        rhs = innermost.right
        if isinstance(rhs, ast.Constant) and isinstance(rhs.value, str):
            continue
        hits.append(n)
    return hits


def test_nobody_hand_rolls_the_run_root_anchor() -> None:
    """存下来的相对路径只能走 `paths.resolve_display_relpath`。

    判据落在**这件事**上而不是某个文件名单：护栏扫全仓，新写的读方默认违规，
    要放行必须在上面两张表里写下理由（`feedback_guardrails_must_scan_not_list`）。
    """
    registered = {rel for rel, _ in _RUN_LOCAL_BY_DESIGN}
    offenders = []
    for f in _production_files():
        rel = f.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue
        for hit in _state_root_joins(tree):
            if rel not in registered:
                offenders.append(f"{rel}:{hit.lineno}  {ast.unparse(hit)[:80]}")

    assert not offenders, (
        "这些地方在手拼 run 根解析一条存下来的相对路径；绑了 Project worktree 时必错，"
        "且只表现为『文件不存在』。改用 core.paths.resolve_display_relpath：\n  "
        + "\n  ".join(offenders)
    )


def test_the_model_dialect_round_trips_too(bound: State) -> None:
    """另一种方言也必须成对：交给模型的路径，模型的工具要能解回同一个文件。

    `stage_hypothesis_draft` 曾经把 `outputs/hypothesis/drafts/x.md` 交给模型，
    而它自己那句"下一步 save_artifact(content_from_file=…)"用同一套工具**永远
    读不到**（绑 worktree 的 run 上 100% 失败）—— 因为框架只有解析器
    `resolve_tool_path`，没有配套的产出函数，想告诉模型"文件在这儿"的地方只能
    自己拼。
    """
    from core.project_workspace import resolve_tool_path, tool_relpath, working_directory

    target = working_directory(bound) / "drafts" / "probe.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")

    rel = tool_relpath(bound, target)
    assert rel == "drafts/probe.md"
    assert resolve_tool_path(bound, rel, write=False) == target.resolve()


def test_staged_draft_is_readable_by_the_tool_it_points_at(tmp_path: Path) -> None:
    """端到端：stage 返回的那个路径，save_artifact 必须真的读得回来。

    这条锚在**效果**上而不是字符串长相上 —— 断言"返回了 drafts/…"证明不了
    下一步走得通（`feedback_asserting_the_label_asserts_nothing`）。
    """
    from nodes.hypothesis.tools.artifact_save import resolve_artifact_content
    from nodes.hypothesis.tools.artifact_staging import stage_draft_content

    state = _bound_state(tmp_path, node_type="hypothesis")
    rel = stage_draft_content(state, "research_plan", "# plan")
    assert resolve_artifact_content(state, content_file=rel) == "# plan"


def test_hypothesis_recovery_finds_the_staged_draft(tmp_path: Path) -> None:
    """恢复路径读的是同一个落点（原来按 run 根拼，永远返回 None）。"""
    from nodes.hypothesis.artifact_recovery import _research_plan_body
    from nodes.hypothesis.tools.artifact_staging import stage_draft_content

    state = _bound_state(tmp_path, node_type="hypothesis")
    stage_draft_content(state, "research_plan", "# plan")
    assert _research_plan_body(state) is not None


def test_input_inspection_uses_the_model_anchor(tmp_path: Path) -> None:
    """data 的输入检查器：模型键入的相对路径锚在它的工作目录，不是进程 cwd。"""
    from core.project_workspace import working_directory
    from nodes.data.tools.input_inspection import _resolve

    state = _bound_state(tmp_path, node_type="data")
    target = working_directory(state) / "cases" / "run.inp"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")

    assert _resolve(state, "cases/run.inp") == target.resolve()
    outside = tmp_path / "external" / "d.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text("a\n", encoding="utf-8")
    assert _resolve(state, str(outside)) == outside.resolve(), "只读检查器不设边界"
