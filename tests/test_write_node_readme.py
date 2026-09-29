"""write_node_readme：只写自己那一个文件，且写的位置正是读端读的位置。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.tools.builtin import _write_node_readme


def _state(tmp_path: Path) -> SimpleNamespace:
    """工具只看 `workspace_records_dir`（= 节点目录本身，如 `<root>/plan`）。"""
    node_dir = tmp_path / "plan"
    node_dir.mkdir(parents=True)
    return SimpleNamespace(workspace_records_dir=node_dir)


@pytest.mark.asyncio
async def test_it_lands_where_the_reader_looks(tmp_path: Path) -> None:
    """读端读 <node_dir>/README.md —— 写端必须写到同一个地方。

    两端锚点分叉过一次（相对路径锚点分叉 PR#707），所以这里并排断言。
    """
    st = _state(tmp_path)
    res = await _write_node_readme(st, content="# hypothesis\n\n本轮锁定四条假说，Q4 未跑。")
    assert res["status"] == "success"

    reader_looks_at = Path(st.workspace_records_dir) / "README.md"
    assert Path(res["path"]) == reader_looks_at
    assert reader_looks_at.read_text(encoding="utf-8").startswith("# hypothesis")


@pytest.mark.asyncio
async def test_it_reports_the_line_the_downstream_will_see(tmp_path: Path) -> None:
    """回执要说清「下游看到的是哪一行」—— 首个非标题行，与读端取法一致。"""
    res = await _write_node_readme(
        _state(tmp_path), content="# 标题\n\n本轮锁定四条假说，Q4 未跑。\n\n更多细节。")
    assert res["handoff_line"] == "本轮锁定四条假说，Q4 未跑。"


@pytest.mark.asyncio
async def test_it_overwrites_wholesale(tmp_path: Path) -> None:
    st = _state(tmp_path)
    await _write_node_readme(st, content="旧的全文")
    await _write_node_readme(st, content="新的全文")
    got = (Path(st.workspace_records_dir) / "README.md").read_text(encoding="utf-8")
    assert got.strip() == "新的全文"


@pytest.mark.asyncio
async def test_empty_content_is_refused_not_written(tmp_path: Path) -> None:
    """非空 = schema required + minLength:1，派发口核（工具体内不再手写）。"""
    from core.bootstrap import bootstrap
    from core.tool_registry import execute

    bootstrap()
    st = _state(tmp_path)
    res = await execute("write_node_readme", st, content="   ")
    assert res["status"] == "error" and res.get("parameter_violations"), res
    assert not (Path(st.workspace_records_dir) / "README.md").exists()


@pytest.mark.asyncio
async def test_unbound_workspace_says_so_instead_of_writing_somewhere_useless() -> None:
    """没绑工作区时，节点目录不存在，写哪都没人读 —— 要说出来，不要假装成功。"""
    res = await _write_node_readme(SimpleNamespace(workspace_records_dir=None),
                                   content="本轮做了什么")
    assert res["status"] == "error"
    assert "工作区" in res["error"]
