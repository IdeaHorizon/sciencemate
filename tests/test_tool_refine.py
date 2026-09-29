"""v1.5 refine 测试：curator_scan + update_memory 调度器。

合并掉的 9 个工具（5 删 + 4 合到 curator_scan + 3 合到 update_memory）的
覆盖测试。原工具的 Python helper 内部仍存在，确保新调度器能正确转发。
"""
from __future__ import annotations

import pytest

from core.state import State

# ── v1.5 refine: curator_scan + update_memory 测试 ───────────────────────────


@pytest.mark.asyncio
async def test_curator_scan_unknown_type_returns_list(tmp_path):
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    from core.state import State
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="p_scan")
    res = await execute_tool("curator_scan", state, scan_type="bogus_type")
    # 契约归 schema：scan_type 的 enum 在 parameters_schema，派发口核一次并把
    # 合法值写进报错；工具体内不再手写 available_scan_types。
    assert res["status"] == "error"
    assert res["parameter_violations"]
    assert "stale_dead_end" in res["error"]


@pytest.mark.asyncio
async def test_curator_scan_mode3_requires_target(tmp_path):
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    from core.state import State
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="p_scan")
    res = await execute_tool("curator_scan", state, scan_type="mode3")
    assert res["status"] == "error"
    assert "target_entity" in res["error"]


@pytest.mark.asyncio
async def test_curator_scan_dispatches_stale_dead_end(tmp_path):
    """走通 stale_dead_end 路径（空 KB）应返 success + 空列表。"""
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    from core.state import State
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="p_scan")
    res = await execute_tool("curator_scan", state,
                             scan_type="stale_dead_end", days=30)
    assert res["status"] == "success"


@pytest.mark.skip(reason=(
    "v2.0：update_memory tool 已删除（v2.1 4-kind memory 退役）。"
    "替代：write_memory_topic（curator）/ add_memory_candidate（agent）。"
))
@pytest.mark.asyncio
async def test_update_memory_unknown_action_returns_list(tmp_path):
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    from core.state import State
    state = State.new(node_type="literature", base_dir=tmp_path,
                      project_id="p_um")
    res = await execute_tool("update_memory", state,
                             memory_id="mem_x", action="bogus",
                             reasoning="testing unknown action")
    assert res["status"] == "error"
    assert "available_actions" in res


@pytest.mark.skip(reason="v2.0: update_memory tool 已删除（同上）。")
@pytest.mark.asyncio
async def test_update_memory_promote_requires_target(tmp_path):
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    from core.state import State
    state = State.new(node_type="literature", base_dir=tmp_path,
                      project_id="p_um")
    res = await execute_tool("update_memory", state,
                             memory_id="mem_x", action="promote",
                             reasoning="testing missing target")
    assert res["status"] == "error"
    assert "target" in res["error"]


@pytest.mark.skip(reason="v2.0: update_memory tool 已删除（同上）。")
@pytest.mark.asyncio
async def test_update_memory_supersede_requires_target(tmp_path):
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    from core.state import State
    state = State.new(node_type="literature", base_dir=tmp_path,
                      project_id="p_um")
    res = await execute_tool("update_memory", state,
                             memory_id="mem_x", action="supersede",
                             reasoning="testing missing target")
    assert res["status"] == "error"
    assert "target" in res["error"]


@pytest.mark.skip(reason="v2.0: update_memory tool 已删除（同上）。")
@pytest.mark.asyncio
async def test_update_memory_archive_no_target_ok(tmp_path):
    """archive 不需要 target —— 应该 dispatch 到 _archive_memory（会因 memory_id
    不存在报别的 error，但已经过了 action validation）。"""
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    from core.state import State
    state = State.new(node_type="literature", base_dir=tmp_path,
                      project_id="p_um")
    res = await execute_tool("update_memory", state,
                             memory_id="mem_nonexist", action="archive",
                             reasoning="testing archive path no target")
    # archive dispatch 成功，但内部因 memory_id 不存在仍可能 error
    # 关键是 not "available_actions" / not "target" error
    assert "available_actions" not in res
