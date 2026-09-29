"""hypothesis singleton artifact 保存策略测试。"""
from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from core.state import State
from nodes.hypothesis.tools.artifact_save import (
    resolve_artifact_content,
    save_hypothesis_singleton,
)


def test_save_strips_v2_and_drops_old_research_plan() -> None:
    state = State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))
    first = save_hypothesis_singleton(state, "research_plan", "tC40_Research_Plan", "draft")
    second = save_hypothesis_singleton(
        state, "research_plan", "tC40_Research_Plan_v2", "final",
    )

    assert first["id"] == "research_plan__tC40_Research_Plan"
    assert second["id"] == "research_plan__tC40_Research_Plan"
    assert second.get("name_normalized_from") == "tC40_Research_Plan_v2"
    assert len(state.list_artifacts("research_plan")) == 1
    # 同一身份的第二版，不是平行身份：账本上没有 `_v2`，盘上也没有它的原生文件
    assert state.artifact_head("research_plan__tC40_Research_Plan_v2") is None
    assert state.artifact_head("research_plan__tC40_Research_Plan").version == 2
    assert not list(state.root.glob("artifacts/research_plan__tC40_Research_Plan_v2.*"))


def test_save_via_content_b64() -> None:
    state = State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))
    body = "Plan sections: experimental_design\n|F\\|<0.01"
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    resolved = resolve_artifact_content(state, content_b64=encoded)
    assert resolved == body
    result = save_hypothesis_singleton(state, "research_plan", "tC40_Research_Plan", resolved)
    assert result["status"] == "success" if "status" in result else result["id"]


def test_save_via_content_file() -> None:
    # 相对路径的锚点 = 模型文件工具的 cwd（working_directory），不是 state.root。
    # 旧契约锚在 state.root —— 那是模型的 write_file 根本不落文件的地方。
    from core.project_workspace import working_directory

    state = State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))
    draft = working_directory(state) / "drafts"
    draft.mkdir(parents=True)
    (draft / "plan.md").write_text("# research plan", encoding="utf-8")
    resolved = resolve_artifact_content(state, content_file="drafts/plan.md")
    assert resolved == "# research plan"


def _bound_state(tmp: Path) -> State:
    """按 2026-08-13 现场的形状搭一个绑了 Git worktree 的 run。"""
    import subprocess

    from core.project_workspace import bind_project_workspace

    worktree = tmp / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    (worktree / "PROJECT.md").write_text("# project", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-m", "init"],
        check=True, env={**__import__("os").environ, **env},
    )
    state = State.new(node_type="hypothesis", base_dir=tmp / "runs")
    bind_project_workspace(state, worktree)
    return state


def test_content_file_in_the_worktree_is_accepted() -> None:
    """现场复盘：模型用 bash 把草稿写进自己的 worktree 目录，然后 content_file
    传绝对路径被拒（"必须在 run 目录内"）、传相对路径找不到 —— 合法空间与模型
    能写文件的空间零交集，一次保存连撞七次。修完之后，两种写法都必须能用。"""
    tmp = Path(tempfile.mkdtemp())
    state = _bound_state(tmp)
    drafts = Path(state.workspace_root) / "drafts"
    drafts.mkdir(parents=True)
    target = drafts / "research_plan.md"
    target.write_text("# plan", encoding="utf-8")

    assert resolve_artifact_content(state, content_file=str(target)) == "# plan"
    assert resolve_artifact_content(state, content_file="drafts/research_plan.md") == "# plan"


def test_content_file_outside_every_boundary_is_still_refused() -> None:
    tmp = Path(tempfile.mkdtemp())
    state = _bound_state(tmp)
    outside = tmp / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    try:
        resolve_artifact_content(state, content_file=str(outside))
    except ValueError:
        pass
    else:
        raise AssertionError("worktree 之外的绝对路径不该被读")


def test_missing_content_file_error_names_the_anchor() -> None:
    """报错必须列合法取值空间 —— 只说"不存在"就是逼模型猜（现场猜了三种拼法）。"""
    tmp = Path(tempfile.mkdtemp())
    state = _bound_state(tmp)
    try:
        resolve_artifact_content(state, content_file="drafts/nope.md")
    except ValueError as exc:
        assert "锚在" in str(exc)
        assert str(state.workspace_root) in str(exc)
    else:
        raise AssertionError("不存在的文件必须报错")
