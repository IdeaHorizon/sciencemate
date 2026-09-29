"""记忆工具面（四个）与它们背后的机械保证。

最重要的一条：**宪法只有用户能立**。用户口述的规矩要能立即生效（不过收件箱），
但正因为它权威，模型不能替用户发明 —— 引文逐字核对是这两个要求的交点。
"""
from __future__ import annotations

import pytest

from core import memory as M
from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute as run_tool


@pytest.fixture
def st(tmp_path, mem_worktree):
    bootstrap()
    s = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs",
                  project_id="tools_test", project_worktree=mem_worktree)
    M.ensure_skeleton(s)
    return s


def _said(st, *texts):
    st.hook_state["_user_utterances"] = list(texts)


# ── 宪法：抽象归模型，出处归框架 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_law_text_is_the_models_abstraction_not_the_users_words(st):
    """核心命题：正文该是**抽象过的**，不是用户原话的复制。

    用户的话零散、口语、跨轮，而且他常常不说"你要长期遵守"，只是反复强调
    同一件事。把它变成能在 review 时逐条回答的规矩，需要模型抽象 ——
    逐字原话根本不是一条能用的铁律。
    """
    _said(st,
          "坚决不能打补丁！！！该重写就他妈彻底重写",
          "遇到删不动的就重构，坚决不能绕过去",
          "不要图省事，不要糊窗户纸")
    law = ("任何修复必须回答：改的是产生问题的那一层还是症状层？"
           "修完之后同类待办会不会自己消失？答不出的 review 不过。")
    res = await run_tool("memory_write", st, section="law", text=law,
                         derived_from=["坚决不能打补丁", "遇到删不动的就重构",
                                       "不要糊窗户纸"])
    assert res["status"] == "success" and res["created"] is True

    entry = M.laws(st)[0]
    assert entry.text == law                      # 落的是抽象版
    assert entry.text not in " ".join(st.hook_state["_user_utterances"])
    assert len(entry.derived_from) == 3           # 出处可审计
    # 立完要让用户看见 —— 他没批准过这次抽象
    assert "告诉用户" in res["next_step"]


@pytest.mark.asyncio
async def test_provenance_must_be_real_user_words(st):
    """模型可以抽象，但不能凭空 —— 出处必须是用户真说过的。"""
    _said(st, "帮我看看这个实验为什么失败了")
    res = await run_tool("memory_write", st, section="law",
                         text="所有实验必须跑够 10 万步",
                         derived_from=["实验一定要跑够十万步"])
    assert res["code"] == "derived_from_not_found"
    assert "不能是你的转述或概括" in res["error"]
    assert M.laws(st) == []


@pytest.mark.asyncio
async def test_law_without_provenance_is_refused(st):
    _said(st, "随便聊聊")
    res = await run_tool("memory_write", st, section="law",
                         text="一条没有出处的规矩")
    assert res["code"] == "derived_from_required"
    assert "凭空立法" in res["error"]


@pytest.mark.asyncio
async def test_provenance_spans_multiple_turns(st):
    """用户是跨轮反复强调的 —— 出处允许来自不同轮次。"""
    _said(st, "这个地方别绕过去", "我说了不要绕，重构掉", "又绕了，重构")
    res = await run_tool("memory_write", st, section="law",
                         text="遇到改不动的结构，重构而不是绕行",
                         derived_from=["别绕过去", "不要绕，重构掉", "又绕了"])
    assert res["status"] == "success"
    assert len(M.laws(st)[0].derived_from) == 3


@pytest.mark.asyncio
async def test_a_law_lands_immediately_without_any_approval_step(st):
    """没有收件箱、没有人审核 —— 下一轮就生效。"""
    from core.memory_delivery import constitution_block

    _said(st, "以后所有实验都要先冻预注册")
    await run_tool("memory_write", st, section="law",
                   text="实验开跑前必须存在已冻结的预注册",
                   derived_from=["所有实验都要先冻预注册"])
    block = constitution_block(st)
    assert block is not None and "已冻结的预注册" in block


# ── 撤销也只有用户能做 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_the_user_can_retire_a_law(st):
    _said(st, "以后所有实验都要先冻预注册")
    await run_tool("memory_write", st, section="law",
                   text="实验开跑前必须存在已冻结的预注册",
                   derived_from=["所有实验都要先冻预注册"])

    # 模型自作主张撤 → 拒
    _said(st, "这次先跑起来看看")
    res = await run_tool("memory_write", st, section="law",
                         retire="实验开跑前")
    assert res["code"] == "only_user_can_retire_a_law"
    assert len(M.laws(st)) == 1

    # 用户说撤 → 可以
    _said(st, "那条预注册的铁律撤了吧")
    res = await run_tool("memory_write", st, section="law",
                         retire="实验开跑前")
    assert res["status"] == "success"
    assert M.laws(st) == []


# ── 叙事：单写者 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_narrative_is_orchestrator_only(st, tmp_path, mem_worktree):
    res = await run_tool("memory_write", st, section="narrative",
                         text="先扫小 L 再外推")
    assert res["status"] == "success"

    other = State.new(node_type="experiment", base_dir=tmp_path / "r2",
                      project_id="tools_test", project_worktree=mem_worktree)
    res2 = await run_tool("memory_write", other, section="narrative",
                          text="我也想写叙事")
    assert res2["code"] == "not_the_owner"
    assert "先扫小 L" in M.read_section(st, M.SECTION_NARRATIVE)


@pytest.mark.asyncio
async def test_manual_sections_are_not_writable_wholesale(st):
    res = await run_tool("memory_write", st, section="manual_pitfall",
                         text="- 我要整节覆写")
    # 手册节不在 schema enum 里：派发口按契约拒，报错列合法节；工具体内无手写检查。
    assert res["status"] == "error" and res["parameter_violations"]
    assert "manual_pitfall" in res["error"] and "goal" in res["error"]


# ── 手册：零门禁 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_note_needs_no_approval(st):
    res = await run_tool("memory_note", st,
                         text="冻结产物前必须先把它登记成 chunk",
                         category="pitfall", tools=["freeze_artifact"])
    assert res["status"] == "success" and res["created"] is True
    assert "登记成 chunk" in M.read_section(st, M.SECTION_PITFALL)


@pytest.mark.asyncio
async def test_note_defaults_applies_to_the_writing_node(st):
    """不填 nodes 时默认本节点 —— 零门禁不等于零结构。"""
    await run_tool("memory_note", st, text="调度前先看局面块里的证据计数")
    e = M.manual_entries(st, section=M.SECTION_PITFALL)[0]
    assert e.nodes == ("_orchestrator",)


# ── 送达：首用附单 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_briefing_attaches_on_first_use_only(st):
    """教训在**动手时刻**弹出；念第二遍只挤占上下文。"""
    await run_tool("memory_note", st,
                   text="save_artifact 之前先确认类型注册过 CANARY_BRIEF",
                   tools=["save_artifact"])
    r1 = await run_tool("save_artifact", st, artifact_type="analysis_report",
                        name="t1", content="x")
    assert "CANARY_BRIEF" in str(r1.get("memory_note_on_this_tool", ""))
    r2 = await run_tool("save_artifact", st, artifact_type="analysis_report",
                        name="t2", content="y")
    assert "memory_note_on_this_tool" not in r2


@pytest.mark.asyncio
async def test_unrelated_tool_gets_no_briefing(st):
    await run_tool("memory_note", st, text="只跟 save_artifact 有关的教训",
                   tools=["save_artifact"])
    r = await run_tool("list_artifacts", st)
    assert "memory_note_on_this_tool" not in r


# ── reviewer 红旗 ───────────────────────────────────────────────────────────


def test_law_checklist_expands_each_law_as_a_question(st):
    from core.memory_delivery import law_checklist

    M.append_law(st, text="第一条：改的是哪一层？", derived_from=["a"])
    M.append_law(st, text="第二条：同类待办会不会自己消失？", derived_from=["b"])
    assert law_checklist(st) == ["第一条：改的是哪一层？",
                                 "第二条：同类待办会不会自己消失？"]


@pytest.mark.asyncio
async def test_reviewer_gets_the_law_checklist_and_others_do_not(
        tmp_path, mem_worktree):
    from core.loader import load_harness
    from core.loop_hooks import HookContext
    from core.loop_hooks_builtin import _law_review_gate_on_turn_start

    bootstrap()
    st = State.new(node_type="_reviewer", base_dir=tmp_path / "rv",
                   project_id="law_test", project_worktree=mem_worktree)
    M.ensure_skeleton(st)
    M.append_law(st, text="改的是产生问题的那一层还是症状层？", derived_from=["a"])

    out = _law_review_gate_on_turn_start(HookContext(
        state=st, harness=load_harness("_reviewer"), turn=1, messages=[]))
    assert out is not None
    body = out[0].content
    assert "逐条回答" in body and "症状层" in body
    assert "遵守 / 违反 / 不适用" in body
    # 红旗不硬闸：措辞必须明确不阻断
    assert "不必因此直接 revise" in body

    st2 = State.new(node_type="experiment", base_dir=tmp_path / "ex",
                    project_id="law_test", project_worktree=mem_worktree)
    assert _law_review_gate_on_turn_start(HookContext(
        state=st2, harness=load_harness("experiment"), turn=1,
        messages=[])) is None


# ── 检索 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_returns_every_layer(st):
    _said(st, "你每次派发之前得先看局面")
    await run_tool("memory_write", st, section="law",
                   text="派发子节点前必须先读研究局面块",
                   derived_from=["派发之前得先看局面"])
    await run_tool("memory_write", st, section="narrative", text="走了 A 路线")
    await run_tool("memory_note", st, text="一条手册教训 RECALLME")

    res = await run_tool("memory_recall", st, query="RECALLME")
    assert "先读研究局面块" in res["law"]
    assert "A 路线" in res["narrative"]
    assert any("RECALLME" in e["text"] for e in res["manual"])
