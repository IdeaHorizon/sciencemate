"""`content_from_file` 有序拼接：段与段之间不能黏成一行。

分段写的人几乎不会记得在每段末尾留换行，而少了它 `\\end{abstract}` 会直接接上
`\\section{Introduction}` —— 对 LaTeX / markdown 都是实质性损坏，且损坏点只在
下游编译或阅读时才暴露，那时已经离产生它的地方很远了。
"""

from __future__ import annotations

import asyncio

from core.bootstrap import bootstrap
from core.state import State

bootstrap()


def _save(st, **kw):
    from core.tool_registry import execute

    return asyncio.run(execute("save_artifact", st, **kw))


def _write(st, name: str, text: str):
    from core.project_workspace import working_directory

    path = working_directory(st) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _body(st, artifact_type: str) -> str:
    records = st.list_artifacts(artifact_type)
    return st.read_artifact(records[-1]["id"])["content"]


def test_segments_without_trailing_newlines_stay_separate(tmp_path):
    st = State.new(node_type="writing", base_dir=tmp_path / "runs", project_id="p_nl")
    _write(st, "a.tex", "\\end{abstract}")      # 无末尾换行
    _write(st, "b.tex", "\\section{Introduction}")

    result = _save(st, artifact_type="scratchpad", name="joined",
                   content_from_file=["a.tex", "b.tex"])
    assert result.get("status") == "success", result
    body = _body(st, "scratchpad")
    assert "\\end{abstract}\\section{Introduction}" not in body, "两段黏在一起了"
    assert "\\end{abstract}\n\\section{Introduction}" in body


def test_existing_newlines_are_not_doubled(tmp_path):
    """已经有换行的不重复加 —— 补齐不等于改写作者的排版。"""
    st = State.new(node_type="writing", base_dir=tmp_path / "runs", project_id="p_nl2")
    _write(st, "a.md", "first\n")
    _write(st, "b.md", "second\n")

    _save(st, artifact_type="scratchpad", name="clean",
          content_from_file=["a.md", "b.md"])
    assert "first\n\nsecond" not in _body(st, "scratchpad")


def test_the_last_segment_keeps_its_own_ending(tmp_path):
    """最后一段不动 —— 不给内容凭空加尾巴。"""
    st = State.new(node_type="writing", base_dir=tmp_path / "runs", project_id="p_nl3")
    _write(st, "a.md", "head\n")
    _write(st, "b.md", "tail-no-newline")

    _save(st, artifact_type="scratchpad", name="tailcheck",
          content_from_file=["a.md", "b.md"])
    assert _body(st, "scratchpad").endswith("tail-no-newline")
