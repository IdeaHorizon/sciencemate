"""Proposal 队列 org/project 归属根因修复回归测试（2026-07 v10c dogfood 实测）。

根因：propose() 之前不论 target KB record 的真实 scope，一律把 proposal 写进
"恰好触发这次扫描/提议的项目"的本地 kb_proposals.jsonl。find_synthesis_candidates
等自动扫描工具用 state.list_kb() 合并读 project+org 两层 KB，扫到的 org 共享
concept（可能来自完全不相关的其它项目——例如一个跑 LJ 流体截断半径课题的
项目，proposal 队列里混进了另一个跑元胞自动机课题的 concept）就这样被塞进了
当前项目的私有队列。实测同一个项目 47 条 proposal 里 42 条（89%）target 的
其实是别的课题的 org 概念。同时因为去重只查本项目文件，同一个 org concept
会被不同项目反复重复提议。

修复：propose() 按 target_id 解析出的 KB record 的实际 scope 路由——
scope=org → 写共享 org/kb_proposals.jsonl；scope=project（或没有真实 KB
target，如 profile/project update）→ 仍写本项目文件。去重 helper
（_read_pending_synthesis_proposal_concepts）同步查两个文件。
"""
from __future__ import annotations

import json

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute as execute_tool
from shared.tools.library.proposals import (
    _kb_proposals_path,
    _org_kb_proposals_path,
)


def _org_seed(**fields) -> dict:
    """一条合法的 org 层记录：必带晋升出处。

    org 没有出生通道 —— 任何在 org 层的东西都是被晋升上来的
    （kb_schema.org_provenance_errors）。测试要造"已在 org"的状态就用这个，
    别手写 {"scope": "org"} —— 那正是护栏要拦的形状。
    """
    return {**fields, "scope": "org", "promoted_from": {
        "project_id": "p_previous", "source_id": "src_previous",
        "approved_by": "test_approver", "at": "2026-08-21T00:00:00Z"}}


async def _make_concept(state: State, name: str, concept_type: str = "method",
                          scope: str | None = None) -> str:
    if scope == "org":
        # org 没有出生通道 —— 直接造一条**已晋升**的记录（带出处），
        # 模拟"上一个项目晋升上来的共享概念"。这正是本文件要测的对象：
        # 扫到别的项目晋升上来的 org 概念时，proposal 该往哪路由。
        rec, _ = state.write_kb("concepts", _org_seed(
            canonical_name=name, concept_type=concept_type,
            description="seed concept for scope-routing tests"))
        return rec["id"]
    res = await execute_tool(
        "create_concept", state,
        canonical_name=name, concept_type=concept_type,
        description="seed concept for scope-routing tests",
    )
    assert res.get("status") == "success", res
    return res["id"]


async def _make_claim(state: State, *, concept_id: str, text_suffix: str = "",
                        scope: str = "project") -> dict:
    res = await execute_tool(
        "create_claim", state,
        claim_text=f"an empirical claim {text_suffix or concept_id}",
        claim_type="empirical", confidence=0.6,
        concept_ids=[concept_id], sources=["doi:10/x"],
        scope=scope,
    )
    assert res.get("status") == "success", f"create_claim failed: {res}"
    return res


@pytest.mark.asyncio
async def test_org_scope_target_proposal_goes_to_org_file_not_project_file(tmp_path):
    """propose() 直接调用一条 kb_other proposal，target 是 scope=org 的 concept
    → 必须落 org/kb_proposals.jsonl，不落本项目文件。"""
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_lj_cutoff")
    org_cid = await _make_concept(state, "CA_grid_size_unrelated_topic", scope="org")

    res = await execute_tool(
        "propose", state,
        proposal_type="kb_other", target_entity="concepts", target_id=org_cid,
        proposed_action="note_something",
        reasoning="test: proposal about an org-scope concept from another project",
    )
    assert res["status"] == "success"
    assert res["routed_to"] == "org_kb"

    org_path = _org_kb_proposals_path()
    proj_path = _kb_proposals_path(state)
    assert org_path.exists()
    org_lines = [json.loads(l) for l in org_path.read_text().splitlines() if l.strip()]
    assert any(p["target_id"] == org_cid for p in org_lines)
    if proj_path.exists():
        proj_lines = [json.loads(l) for l in proj_path.read_text().splitlines() if l.strip()]
        assert not any(p["target_id"] == org_cid for p in proj_lines)


@pytest.mark.asyncio
async def test_project_scope_target_proposal_stays_in_project_file(tmp_path):
    """target 是 scope=project 的 claim → 照旧落本项目文件（不该被误路由到 org）。"""
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_my_own_topic")
    cid = await _make_concept(state, "my_own_method", scope="project")
    claim = await _make_claim(state, concept_id=cid, scope="project")

    res = await execute_tool(
        "propose", state,
        proposal_type="kb_other", target_entity="claims", target_id=claim["id"],
        proposed_action="note_something",
        reasoning="test: proposal about my own project-scope claim",
    )
    assert res["status"] == "success"
    assert res["routed_to"] == "project_kb"

    proj_path = _kb_proposals_path(state)
    assert proj_path.exists()
    proj_lines = [json.loads(l) for l in proj_path.read_text().splitlines() if l.strip()]
    assert any(p["target_id"] == claim["id"] for p in proj_lines)


@pytest.mark.asyncio
async def test_two_projects_scanning_same_org_concept_do_not_duplicate_proposals(tmp_path):
    """核心回归：项目 A 跑 dreaming 对某 org concept 生成 synthesis proposal 后，
    项目 B（完全不同课题）跑同样的扫描不该对同一个 org concept 再生成一条重复
    proposal——因为去重现在会看共享 org 文件，不再只看各自的本地文件。"""
    bootstrap()
    state_a = State.new(node_type="_curator", base_dir=tmp_path,
                          project_id="p_topic_a")
    org_cid = await _make_concept(state_a, "shared_org_phenomenon",
                                    concept_type="phenomenon", scope="org")
    for i in range(3):
        await _make_claim(state_a, concept_id=org_cid, text_suffix=f"a{i}")

    first = await execute_tool(
        "curator_scan", state_a, scan_type="synthesis_candidates", auto_propose=True,
    )
    assert first["proposals_created"] >= 1

    # 项目 B：完全不同的 project_id，但共用同一个 HARNESS_FRAMEWORK_HOME
    # （tmp_path，见 conftest 的 isolate_harness_home），所以能看到同一个 org KB。
    state_b = State.new(node_type="_curator", base_dir=tmp_path,
                          project_id="p_topic_b_unrelated")
    second = await execute_tool(
        "curator_scan", state_b, scan_type="synthesis_candidates", auto_propose=True,
    )
    # 项目 B 不该对同一个 org concept 重复 propose
    assert org_cid not in {c["concept_id"] for c in second["candidates"]}
    assert org_cid in second["skipped_already_pending"]

    org_path = _org_kb_proposals_path()
    org_lines = [json.loads(l) for l in org_path.read_text().splitlines() if l.strip()]
    matching = [p for p in org_lines
                if p["proposal_type"] == "kb_synthesis_candidate"
                and p["target_id"] == org_cid]
    assert len(matching) == 1, (
        f"同一个 org concept 应该只有 1 条 pending synthesis proposal，"
        f"实际 {len(matching)} 条（去重失效）"
    )

    # 项目 B 自己的本地文件不应该有这个和自己课题无关的 proposal
    proj_b_path = _kb_proposals_path(state_b)
    if proj_b_path.exists():
        proj_b_lines = [json.loads(l) for l in
                          proj_b_path.read_text().splitlines() if l.strip()]
        assert not any(p.get("target_id") == org_cid for p in proj_b_lines)


@pytest.mark.asyncio
async def test_list_proposals_tags_origin_layer_and_merges_org_kb(tmp_path):
    """list_proposals 要能看到 org 共享队列的提议（不然没人能 triage），
    且每条要标 _origin_layer 让人分清"我的项目" vs "org 共享积压"。"""
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_triage_view")
    org_cid = await _make_concept(state, "org_concept_for_listing", scope="org")
    proj_cid = await _make_concept(state, "project_concept_for_listing", scope="project")
    proj_claim = await _make_claim(state, concept_id=proj_cid, scope="project")

    await execute_tool(
        "propose", state,
        proposal_type="kb_other", target_entity="concepts", target_id=org_cid,
        proposed_action="a", reasoning="org-scope item for listing test",
    )
    await execute_tool(
        "propose", state,
        proposal_type="kb_other", target_entity="claims", target_id=proj_claim["id"],
        proposed_action="a", reasoning="project-scope item for listing test",
    )

    listed = await execute_tool("list_proposals", state, status="pending")
    by_target = {p["target_id"]: p for p in listed["proposals"]}
    assert by_target[org_cid]["_origin_layer"] == "org_kb"
    assert by_target[proj_claim["id"]]["_origin_layer"] == "project_kb"

    org_only = await execute_tool(
        "list_proposals", state, status="pending", layer_filter="org_kb",
    )
    assert all(p["_origin_layer"] == "org_kb" for p in org_only["proposals"])
    assert org_cid in {p["target_id"] for p in org_only["proposals"]}
    assert proj_claim["id"] not in {p["target_id"] for p in org_only["proposals"]}
