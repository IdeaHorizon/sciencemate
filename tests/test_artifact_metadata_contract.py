"""Regression coverage for issue #130: artifact metadata stays object-shaped."""
from __future__ import annotations

import pytest

from core.state import State
from core.tool_registry import execute
from shared.tools import builtin as _builtin  # noqa: F401 - registers save_artifact


@pytest.mark.asyncio
async def test_tool_accepts_json_object_string_and_persists_dict(tmp_path):
    state = State.new(node_type="writing", base_dir=tmp_path)

    result = await execute(
        "save_artifact", state,
        artifact_type="manuscript", name="valid", content="body",
        metadata='{"preflight_status": "blocked", "count": 0}',
    )

    assert result["status"] == "success"
    record = state.read_artifact(result["id"])
    assert record["metadata"] == {"preflight_status": "blocked", "count": 0}
    assert isinstance(record["metadata"], dict)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        r'{"citation": "\cite{bad}"}',  # invalid JSON escape from the real run
        "[]",
        "null",
        "42",
        '"plain string"',
    ],
)
async def test_tool_rejects_invalid_or_non_object_json_without_writing(
    tmp_path, metadata,
):
    state = State.new(node_type="writing", base_dir=tmp_path)

    result = await execute(
        "save_artifact", state,
        artifact_type="manuscript", name="invalid", content="body",
        metadata=metadata,
    )

    assert result["status"] == "error"
    assert state.list_artifacts() == []


@pytest.mark.parametrize("metadata", ["not-an-object", [], 1, True])
def test_state_persistence_boundary_rejects_non_dict_metadata(tmp_path, metadata):
    state = State.new(node_type="writing", base_dir=tmp_path)

    with pytest.raises(TypeError, match="metadata must be a dict or None"):
        state.save_artifact("manuscript", "invalid", "body", metadata=metadata)

    assert state.list_artifacts() == []


# ↓ 部分测试已随 #627（QC 判定层整体退场，core/quality_checks.py 删除）移除：
#   它们的被测对象是该层本身（_build_state_summary）。层删了测试跟着走。
#   这批 import 断裂曾把整个 collection 挡住 —— 两个各自全绿的 PR 合并后互咬。
