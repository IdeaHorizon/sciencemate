"""issue #166.4：节点副产物目录必须统一走 core.paths，不得再硬编码。

覆盖三件事：
  1. `node_output_dir()` 契约本身（结构 / 校验 / 相对路径 / 读回退）
  2. 各节点**确实**用了它 —— 不是只加了 helper 没人调
  3. 没有人再在 run 根写旧的散装目录名（回归护栏）
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core import paths

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeState:
    """镜像 bind_project_workspace 设好的那几个字段（含 workspace_root）。"""

    def __init__(self, root: Path, worktree: Path | None, node_type: str) -> None:
        self.root = root
        self.project_worktree = worktree
        self.node_type = node_type
        self.workspace_root = None
        if worktree is not None:
            from core.project_workspace import _NODE_WORKSPACES

            owned = _NODE_WORKSPACES.get(node_type)
            if owned and not Path(owned).suffix:
                self.workspace_root = worktree / owned


def _unbound(root: Path, node_type: str = "_probe") -> "_FakeState":
    """无 Project worktree 的 run 替身（CLI / fixture）。

    以前这些用例直接把 `tmp_path` 当 state 传。锚点现在要看
    `state.project_worktree` / `state.node_type`，只给一个根目录算不出来 ——
    而 `Path.root` 恰好是 `"/"`，传错还不会报错。测试也得拿真正的形状。
    """
    return _FakeState(root, None, node_type)



# ── 1. helper 契约 ────────────────────────────────────────────────────────
def test_node_output_dir_structure(tmp_path: Path) -> None:
    d = paths.node_output_dir(_unbound(tmp_path), "postprocess", "figures")
    assert d == tmp_path / "outputs" / "postprocess" / "figures"
    assert not d.exists(), "不传 create 不该有副作用"


def test_node_output_dir_creates_on_demand(tmp_path: Path) -> None:
    d = paths.node_output_dir(_unbound(tmp_path), "writing", "bundles", create=True)
    assert d.is_dir()


def test_node_output_dir_accepts_nested_kind(tmp_path: Path) -> None:
    assert paths.node_output_dir(_unbound(tmp_path), "postprocess", "a/b") == (
        tmp_path / "outputs" / "postprocess" / "a" / "b"
    )


def test_node_output_dir_no_kind_returns_node_root(tmp_path: Path) -> None:
    assert paths.node_output_dir(_unbound(tmp_path), "data") == tmp_path / "outputs" / "data"


@pytest.mark.parametrize(
    ("node_type", "kind"),
    [("..", "x"), ("a/b", "x"), ("", "x"), ("writing", "../../etc")],
)
def test_node_output_dir_rejects_traversal(tmp_path: Path, node_type: str, kind: str) -> None:
    with pytest.raises(ValueError):
        paths.node_output_dir(_unbound(tmp_path), node_type, kind)


def test_there_is_no_third_way_to_spell_a_relative_path() -> None:
    """`node_output_relpath()` 不能回来。

    它按布局硬拼 `outputs/<node>/<kind>`，与产物真正的落点无关 —— 绑了 worktree
    时两个真锚点都对不上。原来的用例之所以绿，是因为它传的是**没绑 worktree**
    的 state，那种情况下三个字符串恰好重合。合法出身只有两种，且各自成对：
    `display_relpath` / `tool_relpath`。
    """
    assert not hasattr(paths, "node_output_relpath")


def test_named_helpers_all_live_under_outputs(tmp_path: Path) -> None:
    for helper, owner in (
        (paths.hypothesis_drafts_dir, "hypothesis"),
        (paths.postprocess_figures_dir, "postprocess"),
        (paths.postprocess_scratch_dir, "postprocess"),
        (paths.postprocess_data_cache_dir, "postprocess"),
        (paths.writing_manuscript_dir, "writing"),
        (paths.writing_bundles_dir, "writing"),
    ):
        state = _unbound(tmp_path, owner)
        assert helper(state).is_relative_to(paths.node_outputs_root(state)), helper.__name__


# ── 3. 向后兼容读回退 ────────────────────────────────────────────────────
def test_prefer_existing_falls_back_to_legacy_layout(tmp_path: Path) -> None:
    legacy = tmp_path / "manuscript_project"
    legacy.mkdir()
    canonical = paths.writing_manuscript_dir(_unbound(tmp_path, 'writing'))
    assert paths.prefer_existing(tmp_path, canonical) == legacy


def test_prefer_existing_falls_back_for_nested_file(tmp_path: Path) -> None:
    legacy = tmp_path / "drafts" / "research_plan.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old", encoding="utf-8")
    canonical = paths.hypothesis_drafts_dir(_unbound(tmp_path, 'hypothesis')) / "research_plan.md"
    assert paths.prefer_existing(tmp_path, canonical) == legacy


def test_prefer_existing_prefers_canonical_when_both_exist(tmp_path: Path) -> None:
    (tmp_path / "figures").mkdir()
    canonical = paths.postprocess_figures_dir(_unbound(tmp_path, 'postprocess'), create=True)
    assert paths.prefer_existing(tmp_path, canonical) == canonical


def test_prefer_existing_returns_canonical_when_nothing_exists(tmp_path: Path) -> None:
    canonical = paths.postprocess_figures_dir(_unbound(tmp_path, 'postprocess'))
    assert paths.prefer_existing(tmp_path, canonical) == canonical


def test_prefer_existing_ignores_paths_outside_run_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere"
    assert paths.prefer_existing(tmp_path, outside) == outside


def test_legacy_map_covers_every_migrated_dir() -> None:
    """每个迁移过的目录都必须登记回退映射，否则旧 run 读不回来。"""
    for helper, legacy in (
        (paths.hypothesis_drafts_dir, "drafts"),
        (paths.postprocess_figures_dir, "figures"),
        (paths.postprocess_scratch_dir, ".postprocess"),
        (paths.writing_manuscript_dir, "manuscript_project"),
        (paths.writing_bundles_dir, "deliverables"),
    ):
        rel = helper(_unbound(Path("/run"))).relative_to(Path("/run")).as_posix()
        assert paths.LEGACY_RUN_SUBDIRS[rel] == legacy


# ── 4. 节点确实用了 helper（不是加了没人调）───────────────────────────────
def _src(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("rel", "helper"),
    [
        ("nodes/hypothesis/tools/artifact_staging.py", "hypothesis_drafts_dir"),
        # B 刀后 postprocess 的路径读写都在 tools/figure.py：display_relpath
        # 写、resolve_display_relpath 读（成对锚点由
        # test_stored_relpaths_resolve_under_a_worktree 钉）。
        ("nodes/postprocess/tools/figure.py", "display_relpath"),
        ("nodes/postprocess/tools/figure.py", "resolve_display_relpath"),
        # #318 起 data package 终点路径由 package_publisher.package_paths 统一计算
        # （scientific_preprocessor 改走 prepare_package_staging，不再自算路径）
        ("nodes/data/tools/package_publisher.py", "data_package_dir"),
    ],
)
def test_node_calls_paths_helper(rel: str, helper: str) -> None:
    assert f"paths.{helper}(" in _src(rel), f"{rel} 没走 core.paths.{helper}()"


#: 迁移过的文件里不允许再出现的硬编码 run 子目录写法。
_BANNED = re.compile(
    r"""(state|ctx\.state)\.root\s*/\s*["'](drafts|figures|deliverables|"""
    r"""manuscript_project|\.postprocess|latex_build)["']"""
)

_MIGRATED_FILES = [
    "nodes/hypothesis/tools/artifact_staging.py",
    "nodes/postprocess/tools/figure.py",
]


@pytest.mark.parametrize("rel", _MIGRATED_FILES)
def test_no_hardcoded_run_subdir(rel: str) -> None:
    hit = _BANNED.search(_src(rel))
    assert hit is None, f"{rel} 又硬编码了 run 子目录：{hit.group(0) if hit else ''}"


# ── 5. 收编 $HOME 硬编码路径 ─────────────────────────────────────────────
def test_literature_cache_lives_under_framework_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HARNESS_LITERATURE_HOME", raising=False)
    for d in (
        paths.literature_papers_dir(),
        paths.literature_index_dir(),
        paths.literature_cache_dir(),
        paths.literature_credentials_dir(),
    ):
        assert d.is_relative_to(paths.home()), d


def test_literature_home_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path / "lit"))
    assert paths.literature_root() == tmp_path / "lit"


def test_cas_ranking_env_override(tmp_path: Path, monkeypatch) -> None:
    xlsx = tmp_path / "ranking.xlsx"
    xlsx.write_bytes(b"x")
    monkeypatch.setenv("HARNESS_CAS_JOURNAL_RANKING", str(xlsx))
    assert paths.cas_journal_ranking_path() == xlsx


def test_cas_ranking_missing_returns_none_with_actionable_hint(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HARNESS_CAS_JOURNAL_RANKING", str(tmp_path / "nope.xlsx"))
    assert paths.cas_journal_ranking_path() is None
    hint = paths.cas_journal_ranking_hint()
    assert "HARNESS_CAS_JOURNAL_RANKING" in hint


@pytest.mark.parametrize(
    ("rel", "banned"),
    [
        ("nodes/literature/tools/scihub_fetcher.py", 'os.path.expanduser("~/survey-harness'),
        ("nodes/literature/tools/cnki_search.py", 'os.path.expanduser("~/.hermes'),
        ("nodes/literature/tools/cas_ranking.py", "中科院期刊分区表excel完整版.xlsx"),
    ],
)
def test_no_hardcoded_home_path(rel: str, banned: str) -> None:
    """$HOME 硬编码只允许留在 core/paths.py 的 legacy 回退表里。"""
    assert banned not in _src(rel), f"{rel} 仍硬编码 {banned}"


def test_generated_training_script_writes_to_explicit_dir() -> None:
    """生成的 train_model.py 不能相对 cwd 写产物（落点取决于谁启动的进程）。"""
    from nodes.data.tools.scientific_preprocessor import _training_script

    script = _training_script()
    ast.parse(script)  # 模板本身得是合法 Python
    assert 'Path("model_metrics.json")' not in script
    assert 'Path("model_coefficients.json")' not in script
    assert "--out-dir" in script
    assert "Path(__file__).resolve().parent" in script


# ── 6. .gitignore 覆盖 e2e 残留 ──────────────────────────────────────────
@pytest.mark.parametrize("pattern", ["/rdf_rc25_trunc.dat", "/workspace/"])
def test_gitignore_covers_e2e_leftovers(pattern: str) -> None:
    lines = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert pattern in lines


# ── 7. v2.1 锚点：绑了 Project worktree 就落节点自己的 Git 目录 ──────────
#
# 平台把 run 根放在 `<worktree>/.research/cache/` 里 —— 那是 gitignored 的缓存。
# 副产物（图 / manuscript 工程 / 投稿包）锚在 run 根上就出不了这一轮 run：
# checkpoint 不收、publish 不带、下一个 session 的 worktree 里一张图都没有。
# E2E v19 实测：论文里两张 `\includegraphics` 的 PNG 只存在于上一个 session 的
# cache 目录里，源文件在新 session 中完全不存在。
def _worktree_state(tmp_path: Path, node_type: str) -> _FakeState:
    worktree = tmp_path / "worktree"
    cache_run = worktree / ".research" / "cache" / "runtime" / "runs" / "r1"
    cache_run.mkdir(parents=True)
    return _FakeState(cache_run, worktree, node_type)


@pytest.mark.parametrize(
    ("node_type", "helper", "kind"),
    [
        ("postprocess", paths.postprocess_figures_dir, "figures"),
        ("postprocess", paths.postprocess_scratch_dir, "scratch"),
        ("postprocess", paths.postprocess_data_cache_dir, "data_cache"),
        ("hypothesis", paths.hypothesis_drafts_dir, "drafts"),
        ("writing", paths.writing_bundles_dir, "bundles"),
    ],
)
def test_outputs_land_in_node_git_dir_not_cache(
    tmp_path: Path, node_type: str, helper, kind: str,
) -> None:
    from core.project_workspace import _NODE_WORKSPACES

    state = _worktree_state(tmp_path, node_type)
    d = helper(state)
    owned = _NODE_WORKSPACES[node_type]
    expected = tmp_path / "worktree" / owned
    # 目录名已经说明了它是什么时不再重复一层（postprocess 的目录就叫 figures/）。
    if kind != Path(owned).name:
        expected = expected / kind
    assert d == expected
    assert ".research" not in d.parts, "副产物不能落在 gitignored 缓存里"


def test_cross_node_read_resolves_to_owner_dir(tmp_path: Path) -> None:
    """writing 要引 postprocess 的图 —— 解析结果必须是 postprocess 的目录。"""
    state = _worktree_state(tmp_path, "writing")
    assert paths.postprocess_figures_dir(state) == tmp_path / "worktree" / "figures"


def test_experiment_outputs_go_through_the_same_funnel(tmp_path: Path) -> None:
    """experiment 不许再自带一份锚点逻辑（曾经抄了两版，互相打架）。"""
    from nodes.experiment.tools.path_roles import experiment_output_dir

    state = _worktree_state(tmp_path, "experiment")
    assert experiment_output_dir(state, "fast") == (
        tmp_path / "worktree" / "experiments" / "fast"
    )


def test_file_scoped_node_keeps_run_local_outputs(tmp_path: Path) -> None:
    """curator 的作用域是一个文件（MEMORY.md），没有产物目录 —— 退回 run 根。"""
    state = _worktree_state(tmp_path, "_curator")
    d = paths.node_output_dir(state, "_curator")
    assert d == state.root / "outputs" / "_curator"


def test_unbound_run_keeps_run_local_outputs(tmp_path: Path) -> None:
    """CLI / fixture 这类没有 Project worktree 的 run 行为不变。"""
    state = _FakeState(tmp_path / "run", None, "postprocess")
    assert paths.postprocess_figures_dir(state) == (
        tmp_path / "run" / "outputs" / "postprocess" / "figures"
    )


def test_worktree_anchor_matches_working_directory(tmp_path: Path) -> None:
    """模型键入的相对路径和工具算出的产物路径必须落在同一个锚点上。

    两者分叉的代价实测过：prepare 把 manuscript 建在 `<writing>/outputs/x`，
    compile/stage 按 `working_directory()` 找 `<writing>/x` —— 谁都没错，就是
    对不上，图永远进不了稿子。
    """
    from core.project_workspace import working_directory

    state = _worktree_state(tmp_path, "writing")
    state.workspace_root = tmp_path / "worktree" / "paper"
    assert paths.node_output_dir(state, "writing", "sci_manuscript") == (
        working_directory(state) / "sci_manuscript"
    )
