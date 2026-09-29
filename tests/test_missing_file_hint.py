"""文件不存在时要给出**真实存在的邻居**，不能只说"找不到"。

E2E v18 实测：agent 反复去 `.research/orchestration/experiment/README.md`、
`.research/orchestration/MEMORY.md` 找东西——真实位置是工作区根下的
`experiments/README.md` 和 `MEMORY.md`。报错只有一句"找不到 <路径>"，它只能
继续猜，连挂 5 次。

项目铁律：**报错必须列出正确答案**。
"""

from pathlib import Path

from shared.tools.builtin import _missing_file_hint


class _State:
    def __init__(self, root):
        self.workspace_root = root


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "experiments").mkdir(parents=True)
    (root / "plan").mkdir()
    (root / "experiments" / "README.md").write_text("# experiment\n")
    (root / "MEMORY.md").write_text("# memory\n")
    (root / ".research" / "orchestration").mkdir(parents=True)
    (root / ".research" / "orchestration" / "sessions").mkdir()
    return root


def test_hint_lists_what_actually_exists_in_the_nearest_real_dir(tmp_path):
    root = _worktree(tmp_path)
    missing = root / ".research" / "orchestration" / "experiments" / "README.md"
    hint = _missing_file_hint(_State(root), missing)
    # 最近的**存在**祖先是 .research/orchestration，把它下面真有什么列出来
    assert "orchestration" in hint
    assert "sessions/" in hint


def test_hint_points_at_the_real_location_of_that_filename(tmp_path):
    root = _worktree(tmp_path)
    missing = root / ".research" / "orchestration" / "MEMORY.md"
    hint = _missing_file_hint(_State(root), missing)
    assert "MEMORY.md" in hint
    assert "工作区里叫" in hint


def test_readme_hint_finds_the_node_directory_copy(tmp_path):
    root = _worktree(tmp_path)
    missing = root / ".research" / "orchestration" / "plan" / "README.md"
    hint = _missing_file_hint(_State(root), missing)
    assert "experiments/README.md" in hint.replace("\\", "/")


def test_no_workspace_still_returns_something_safe(tmp_path):
    """没有工作区上下文时不能抛异常，也不该编造。"""

    class _Bare:
        pass

    hint = _missing_file_hint(_Bare(), tmp_path / "nope" / "x.txt")
    assert isinstance(hint, str)


def test_an_empty_directory_is_itself_the_answer(tmp_path):
    """⚠️ 判据已翻转（2026-08-21 事故）：空目录**必须**说出来。

    这条测试原本叫 `test_hint_is_empty_when_nothing_useful_to_say`，断言
    "祖先目录存在但为空 → 不硬凑内容"。它固化的正是那场事故的成因：

    curator 去读 `.research/orchestration/artifacts/curator_dreaming_report.md`，
    那个目录当时是**空的**，于是提示被静默跳过，模型拿到光秃秃一句"找不到文件"
    —— 连"这个目录是空的"都没告诉它。它只能继续猜同一个名字，连猜 64 次，
    最后被熔断停机。

    "该目录存在且为空"不是"没有有用信息"，它是**一次就能排除整棵子树**的确定
    事实。把它咽下去，等于扣住了模型唯一的破局信息。原判据把"没编造"和
    "没说话"当成了同一件事 —— 而对模型来说，"我查了，没有"和"我没查"是天差
    地别的两回事。
    """
    class _Bare:
        pass

    (tmp_path / "empty").mkdir()
    hint = _missing_file_hint(_Bare(), tmp_path / "empty" / "x.txt")
    assert "空" in hint, f"空目录被静默跳过了：{hint!r}"
    assert str(tmp_path / "empty") in hint, "没说清楚是哪个目录空"
