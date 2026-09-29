"""判决拆除·第三波：update_claim_status 的裁决资格走**同一条降落路径**。

翻 validated/refuted 时「够不够格」有四个来源：Analysis 没背书、预注册承诺
没兑现、本 run 声明了 infeasible、没挂证据（或 hypothesis 只拿别的 claim 互证）。
前两个早已降落 provisional；后两个原来仍是拒绝（kb.py 的 infeasible 闸、
kb_schema.validate_status_flip 的 evidence 数量子句）。现在四个同路：**从不
以 validated/refuted 入账、也从不拒绝**，authority_note 写明差额，补齐后可再翻。

同 session 反复翻同一 claim 是 thrash **信号**：只记（transcript + 返回值），
不拦——第 4 次翻转可能正是新实验结果，review_history append-only 已留痕。

每条测试用从前会被拒的输入调用；墙若被加回去就转红。
"""
from __future__ import annotations

import json

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute as execute_tool

bootstrap()


def _events(state: State, name: str) -> list[dict]:
    return [json.loads(x) for x in
            state.transcript_path.read_text(encoding="utf-8").splitlines()
            if x.strip() and json.loads(x).get("event") == name]


async def _open_claim(state: State, claim_type: str = "methodological") -> str:
    concept = await execute_tool("create_concept", state,
                                 canonical_name=f"concept-{claim_type}",
                                 concept_type="method", description="seed")
    kwargs = dict(claim_text=f"a {claim_type} claim", claim_type=claim_type,
                  confidence=0.5, concept_ids=[concept["id"]],
                  sources=["doi:10/a", "doi:10/b"])
    if claim_type == "hypothesis":
        chunk, _ = state.write_kb("chunks", {
            "text": "prereg", "source": "artifact://prereg_h1",
            "origin_artifact_frozen": True, "origin_artifact_id": "pre_registration__P",
        })
        kwargs.update(prereg_chunk_id=chunk["id"], hypothesis_id="H1",
                      sources=[chunk["id"]],
                      falsification_criteria_text="若 ratio < 2 则证伪",
                      predicted_outcome="ratio > 2")
    res = await execute_tool("create_claim", state, **kwargs)
    assert res["status"] == "success", res
    return res["id"]


@pytest.mark.asyncio
async def test_validated_without_evidence_lands_provisional(tmp_path):
    # _curator：不受 Analysis 背书 / 预注册 / infeasible 三道资格影响，只剩证据这一道
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_ev_none")
    cid = await _open_claim(state)
    res = await execute_tool("update_claim_status", state, claim_id=cid,
                             new_status="validated",
                             reasoning="复审认为方法学结论成立（但没登记任何证据）")
    assert res["status"] == "success", res
    assert res["landed_status"] == "provisional"
    assert "verdict_evidence: none" in res["authority_note"]
    assert state.get_kb_record("claims", cid)["status"] == "provisional"
    ev = _events(state, "scientific_verdict_downgraded_to_provisional")
    assert ev and ev[-1]["verdict_evidence"] == "none"


@pytest.mark.asyncio
async def test_hypothesis_refuted_on_claims_only_evidence_lands_provisional(tmp_path):
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_ev_claims")
    hid = await _open_claim(state, "hypothesis")
    other = await _open_claim(state, "methodological")
    res = await execute_tool("update_claim_status", state, claim_id=hid,
                             new_status="refuted", evidence_ids=[other],
                             reasoning="另一条 claim 与之矛盾，据此判 refuted")
    assert res["status"] == "success", res
    assert res["landed_status"] == "provisional"
    assert "verdict_evidence: claims_only" in res["authority_note"]
    assert state.get_kb_record("claims", hid)["status"] == "provisional"


@pytest.mark.asyncio
async def test_validated_with_chunk_evidence_still_lands_validated(tmp_path):
    """回归保护：证据齐的翻转照常入账 validated —— 降落不是一刀切。"""
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_ev_ok")
    cid = await _open_claim(state)
    chunk, _ = state.write_kb("chunks", {"text": "evidence", "source": "doi:10/e"})
    res = await execute_tool("update_claim_status", state, claim_id=cid,
                             new_status="validated", evidence_ids=[chunk["id"]],
                             reasoning="独立来源的证据支持该结论")
    assert res["status"] == "success", res
    assert "landed_status" not in res
    assert state.get_kb_record("claims", cid)["status"] == "validated"


@pytest.mark.asyncio
async def test_missing_evidence_id_is_still_a_contract_error(tmp_path):
    """引用不存在的 KB 记录是契约违约（C），不是资格不够——照拒，报错给登记路径。"""
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_ev_missing")
    cid = await _open_claim(state)
    res = await execute_tool("update_claim_status", state, claim_id=cid,
                             new_status="validated", evidence_ids=["chunk_deadbeefdead"],
                             reasoning="引用了一条不存在的 chunk")
    assert res["status"] == "error" and "不存在" in res["error"]


@pytest.mark.asyncio
async def test_fourth_flip_in_a_session_is_recorded_not_refused(tmp_path):
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_thrash")
    cid = await _open_claim(state)
    state.hook_state.setdefault("session_claim_flips", {})[cid] = 3   # 已翻 3 次
    res = await execute_tool("update_claim_status", state, claim_id=cid,
                             new_status="provisional",
                             reasoning="新到的实验结果要求再改一次判断")
    assert res["status"] == "success", res
    assert res["thrash_signal"] is True
    assert res["session_flips_so_far"] == 4
    ev = _events(state, "claim_status_thrash_signal")
    assert ev and ev[-1]["claim_id"] == cid
