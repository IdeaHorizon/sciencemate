"""kb_provenance 得真到得了模型手上 —— 注册 ≠ 接上路径。

节点工具是白名单制：register_tool 只是让它存在，节点 harness.yaml 没列就永远
不出现在工具面上。这类"机制存在但没接到路径"的失效不报错，只是那道防线
从来不响。所以这里按**调用链**验，不按文件名验。
"""
from __future__ import annotations

import pytest

from core.bootstrap import bootstrap
from core.loader import load_harness
from core.state import State


ARCH_NODES_THAT_NEED_IT = ("_reviewer", "_curator")


@pytest.mark.parametrize("node_type", ARCH_NODES_THAT_NEED_IT)
def test_tool_is_on_the_node_surface(node_type):
    bootstrap()
    h = load_harness(node_type)
    names = {getattr(t, "name", t) for t in (getattr(h, "tools", None) or [])}
    assert "kb_provenance" in names, (
        f"{node_type} 的工具面上没有 kb_provenance —— 注册了但没接到路径。"
        f"现有：{sorted(n for n in names if 'kb' in str(n))}")


@pytest.mark.asyncio
async def test_tool_executes_through_the_registry(tmp_path):
    """从注册表真调一次，别只验它在名单里。"""
    from core.tool_registry import execute as execute_tool

    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="prov_reach")
    res = await execute_tool("kb_provenance", state, kb_id="claim_notthere99")
    assert res["status"] == "error"
    assert res["code"] == "not_found"


@pytest.mark.asyncio
async def test_broken_chain_is_reported_not_swallowed(tmp_path):
    """断链要能被看见 —— 这是这个工具存在的全部理由。"""
    from core.tool_registry import execute as execute_tool

    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="prov_broken")
    claim, _ = state.write_kb("claims", {
        "claim_text": "临界温度落在 2.26–2.28 之间",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project",
        "sources": ["chunk_deadbeef1234"],      # 指向不存在的 chunk
        "confidence": 0.7,
    })
    res = await execute_tool("kb_provenance", state, kb_id=claim["id"])
    assert res["status"] == "success"
    assert res["intact"] is False
    assert "chunk_deadbeef1234" in res["broken"]


# ── P4 的两个工具同理 ───────────────────────────────────────────────────────
#
# 2026-08-21 真实数据回放发现：org_dreaming / write_org_canon 写完之后，
# 全仓**没有任何调用方**（只有测试在调）。整个 P4 层是一个 curator 永远
# 触发不到的机制 —— 不报错，只是从来不运行。


@pytest.mark.parametrize("tool_name", ["org_dreaming", "write_org_canon"])
def test_org_maintenance_tools_are_on_curator_surface(tool_name):
    bootstrap()
    h = load_harness("_curator")
    names = {getattr(t, "name", t) for t in (getattr(h, "tools", None) or [])}
    assert tool_name in names, (
        f"_curator 拿不到 {tool_name} —— org 治理层没有触发入口")


@pytest.mark.asyncio
async def test_org_dreaming_runs_through_the_registry(tmp_path):
    from core.tool_registry import execute as execute_tool

    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="org_maint")
    res = await execute_tool("org_dreaming", state)
    assert res["status"] == "success"
    assert len(res["jobs"]) == 5, "五道作业是有界 checklist 的定义，少一道就不是它了"


@pytest.mark.asyncio
async def test_canon_written_through_the_registry_is_read_back_by_orientation(tmp_path):
    """写正典 → 开题注入读得到。两端接上才算这条路通了。"""
    from core.org_delivery import org_orientation
    from core.tool_registry import execute as execute_tool

    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="canon_roundtrip")
    body = "# 自旋模型的有限尺度标度\n\n共识：Binder 累积量交点给出 T_c…"
    res = await execute_tool("write_org_canon", state,
                             domain="统计物理 / 临界现象", body=body,
                             absorbed_ids=["claim_abc123def456"])
    assert res["status"] == "success"
    assert res["version"] == 1

    injected = org_orientation(state, domain="统计物理 / 临界现象")
    assert injected is not None, "正典写进去了，开题注入却读不到 —— 两端没接上"
    assert "Binder" in injected


@pytest.mark.asyncio
async def test_empty_canon_body_is_refused_loudly(tmp_path):
    """机械层写不出叙述 —— 空正文必须报错，不能落一版空综述。"""
    from core.tool_registry import execute as execute_tool

    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="canon_empty")
    res = await execute_tool("write_org_canon", state, domain="d", body="   ")
    assert res["status"] == "error"
    assert res["code"] == "empty_canon"
