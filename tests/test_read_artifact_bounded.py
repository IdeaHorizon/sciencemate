"""read_artifact 的输出必须有界 —— 与 KB 读取、read_file 同一条不变量。

E2E v20 实测：一个 1.8 MB 的 V-T 平表 artifact 让 postprocess 的**第一次** LLM
调用直接 HTTP 400（253953 input tokens / 262144 上限），整个 run 当场 error。
报错说的是"上下文超了"，完全看不出是哪个工具把它撑爆的。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute
from shared.tools.builtin import _MAX_ARTIFACT_CHARS


def _state(tmp_path: Path) -> State:
    bootstrap(force=True)
    return State.new(node_type="postprocess", base_dir=tmp_path / "runs")


def test_small_artifact_is_returned_whole(tmp_path: Path) -> None:
    state = _state(tmp_path)
    saved = state.save_artifact("clean_results", "small", json.dumps({"a": 1}))
    result = asyncio.run(execute("read_artifact", state, artifact_id=saved["id"]))
    assert result["status"] == "success"
    assert result["truncated"] is False
    assert json.loads(result["artifact"]["content"]) == {"a": 1}


def test_huge_artifact_is_capped_and_says_so(tmp_path: Path) -> None:
    state = _state(tmp_path)
    payload = "x" * (_MAX_ARTIFACT_CHARS * 3)
    saved = state.save_artifact("clean_results", "huge", payload)

    result = asyncio.run(execute("read_artifact", state, artifact_id=saved["id"]))

    assert result["status"] == "success"
    assert result["truncated"] is True, "超限必须明说，不能静默截断"
    assert len(result["artifact"]["content"]) == _MAX_ARTIFACT_CHARS
    assert result["total_chars"] == len(payload)
    assert result["next_offset"] == _MAX_ARTIFACT_CHARS
    # 截断了就必须**指名**正确做法，否则模型只会反复 offset 把上下文读爆。
    assert "execute_python" in result["instruction"]
    assert Path(result["file_path"]).is_file()


def test_offset_reads_a_later_window(tmp_path: Path) -> None:
    state = _state(tmp_path)
    payload = "a" * _MAX_ARTIFACT_CHARS + "b" * 100
    saved = state.save_artifact("clean_results", "offsetable", payload)

    tail = asyncio.run(
        execute("read_artifact", state, artifact_id=saved["id"], offset=_MAX_ARTIFACT_CHARS)
    )

    assert tail["artifact"]["content"] == "b" * 100
    assert tail["next_offset"] is None, "读到结尾就不该再给下一段"


def test_output_size_is_independent_of_artifact_size(tmp_path: Path) -> None:
    """同一条第一性原理：工具输出不随被读对象的大小增长。"""
    state = _state(tmp_path)
    sizes = []
    for factor in (2, 8):
        saved = state.save_artifact(
            "clean_results", f"grow{factor}", "y" * (_MAX_ARTIFACT_CHARS * factor)
        )
        out = asyncio.run(execute("read_artifact", state, artifact_id=saved["id"]))
        sizes.append(len(json.dumps(out, ensure_ascii=False)))
    assert abs(sizes[0] - sizes[1]) < 2000, f"输出随产物变大了: {sizes}"


def test_file_path_points_at_the_same_record_that_was_read(tmp_path: Path) -> None:
    """路径和内容必须来自同一份文件，否则 execute_python 会去算另一个东西。"""
    state = _state(tmp_path)
    saved = state.save_artifact("clean_results", "pathcheck", "z" * (_MAX_ARTIFACT_CHARS + 5))
    result = asyncio.run(execute("read_artifact", state, artifact_id=saved["id"]))
    # 正文就是文件：file_path 指向原生文件，读出来就是 content 本身
    on_disk = Path(result["file_path"]).read_text(encoding="utf-8")
    assert on_disk.startswith(result["artifact"]["content"][:1000])
