"""P3c：research_state —— Analysis 的版本化研究状态。

判据全部机械化，所以这里逐条把"绕过路径"钉死：
版本号不由模型报、假说不得凭空消失、裁决必须挂证据、冻结 prereg 假说必须
在册、ready_candidate 不得带着未裁决假说、通用 save_artifact 造不出来。
"""
from __future__ import annotations

import json

import pytest

from core.state import State
from nodes.hypothesis.tools.research_state import (
    VERDICTS,
    _update_research_state,
    _read_research_state,
    frozen_prereg_hypothesis_ids,
    load_versions,
    validate_update,
)


def _state(tmp_path) -> State:
    return State(run_id="r1", node_type="hypothesis", root=tmp_path / "run")


def _freeze_prereg(state: State, ids: list[str]) -> None:
    """冻结的事实只有账本 freeze 行一个出处（`mark_frozen`）；save 里手写
    `frozen` 会被剥掉，`hypothesis_ids` 这类普通 metadata 照留。"""
    saved = state.save_artifact(
        "pre_registration",
        "PreReg",
        "\n".join(f"### {h} something falsifiable" for h in ids),
        metadata={"hypothesis_ids": ids},
    )
    state.mark_frozen(saved["id"])


# ── 版本链 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_version_is_computed_not_reported(tmp_path):
    state = _state(tmp_path)
    first = await _update_research_state(
        state, verdict="continue",
        hypotheses=[{"id": "H1", "status": "active"}],
    )
    assert first["status"] == "success"
    assert first["version"] == 1 and first["parent_version"] is None

    second = await _update_research_state(
        state, verdict="continue", change_reason="补了一条对照假说",
        hypotheses=[{"id": "H1", "status": "active"}, {"id": "H2", "status": "active"}],
    )
    assert second["version"] == 2 and second["parent_version"] == 1
    assert [v for v, _ in load_versions(state)] == [1, 2]


@pytest.mark.asyncio
async def test_missing_change_reason_is_surfaced_not_refused(tmp_path):
    """判决拆除批 3w（research_state.py:204 降格→H-OB1）：有父版本没写
    change_reason 照写入，缺项如实进 metadata.advisories 随版本走。"""
    state = _state(tmp_path)
    await _update_research_state(
        state, verdict="continue", hypotheses=[{"id": "H1", "status": "active"}])
    result = await _update_research_state(
        state, verdict="continue", hypotheses=[{"id": "H1", "status": "active"}])
    assert result["status"] == "success"
    assert any("change_reason" in a for a in result["advisories"])
    record = state.read_artifact("research_state__research_state")
    meta = record["metadata"]
    if isinstance(meta, str):
        import json as _json
        meta = _json.loads(meta)
    assert any("change_reason" in a for a in meta["advisories"])


# ── 假说不得凭空消失 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parent_hypothesis_cannot_silently_vanish(tmp_path):
    state = _state(tmp_path)
    await _update_research_state(
        state, verdict="continue",
        hypotheses=[{"id": "H1", "status": "active"}, {"id": "H2", "status": "active"}])

    dropped = await _update_research_state(
        state, verdict="continue", change_reason="聚焦 H1",
        hypotheses=[{"id": "H1", "status": "active"}])
    assert dropped["status"] == "error"
    assert any("H2" in p for p in dropped["problems"])

    # 显式撤回 + 理由 → 合法出口
    withdrawn = await _update_research_state(
        state, verdict="continue", change_reason="聚焦 H1",
        hypotheses=[
            {"id": "H1", "status": "active"},
            {"id": "H2", "status": "withdrawn", "withdrawn_reason": "所需设备不可用"},
        ])
    assert withdrawn["status"] == "success"


@pytest.mark.asyncio
async def test_withdrawn_reason_split_by_frozen_commitment(tmp_path):
    """判决拆除批 3w（research_state.py:200 拆条）：**冻结 prereg 约束的条目**
    撤回必须写 reason——升 B 保留（withdrawn_reason 就是 S4 申报机制，不申报
    账变假）；未冻结的工作假说降格为 advisory，照写入。"""
    state = _state(tmp_path)
    unfrozen = await _update_research_state(
        state, verdict="continue",
        hypotheses=[{"id": "H1", "status": "withdrawn"}])
    assert unfrozen["status"] == "success"
    assert any("withdrawn_reason" in a for a in unfrozen["advisories"])

    state2 = _state(tmp_path / "frozen")
    _freeze_prereg(state2, ["H1"])
    frozen = await _update_research_state(
        state2, verdict="continue",
        hypotheses=[{"id": "H1", "status": "withdrawn"}])
    assert frozen["status"] == "error"
    assert any("withdrawn_reason" in p for p in frozen["problems"])


# ── 裁决必须挂证据 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_on_hypothesis_requires_evidence(tmp_path):
    state = _state(tmp_path)
    bare = await _update_research_state(
        state, verdict="continue",
        hypotheses=[{"id": "H1", "status": "refuted"}])
    assert bare["status"] == "error"
    assert any("evidence" in p for p in bare["problems"])

    ok = await _update_research_state(
        state, verdict="continue",
        hypotheses=[{"id": "H1", "status": "refuted",
                     "evidence": ["experiment_log__cooling_sweep"]}])
    assert ok["status"] == "success"


@pytest.mark.asyncio
async def test_inconclusive_does_not_require_evidence(tmp_path):
    """「做了但没结论」常常正是证据不足；强行要证据会逼出编造。"""
    state = _state(tmp_path)
    result = await _update_research_state(
        state, verdict="continue",
        hypotheses=[{"id": "H1", "status": "inconclusive"}])
    assert result["status"] == "success"


# ── 冻结 prereg 不可无视 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_frozen_prereg_hypotheses_must_all_be_listed(tmp_path):
    state = _state(tmp_path)
    _freeze_prereg(state, ["H1", "H2", "H3"])
    assert frozen_prereg_hypothesis_ids(state) == {"H1", "H2", "H3"}

    partial = await _update_research_state(
        state, verdict="continue",
        hypotheses=[{"id": "H1", "status": "active"}])
    assert partial["status"] == "error"
    assert any("H2" in p and "H3" in p for p in partial["problems"])


def test_frozen_ids_fall_back_to_scanning_content(tmp_path):
    """没有结构化 hypothesis_ids 时扫正文 —— 要求模型申报就会漏。"""
    state = _state(tmp_path)
    saved = state.save_artifact(
        "pre_registration", "PreReg",
        "## H1 冷却速率\n...\n## H2 势能\n")
    state.mark_frozen(saved["id"])
    assert frozen_prereg_hypothesis_ids(state) == {"H1", "H2"}


def test_unfrozen_prereg_is_not_binding(tmp_path):
    state = _state(tmp_path)
    state.save_artifact("pre_registration", "Draft", "## H9 草稿", metadata={})
    assert frozen_prereg_hypothesis_ids(state) == set()


# ── verdict ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ready_candidate_with_unresolved_entries_is_surfaced(tmp_path):
    """判决拆除批 3w（research_state.py:232 降格→H-OB1）：带未裁决条目宣布
    ready_candidate 照写入——状态差如实进 advisories，reviewer/referee 终审。"""
    state = _state(tmp_path)
    _freeze_prereg(state, ["H1", "H2"])
    surfaced = await _update_research_state(
        state, verdict="ready_candidate",
        hypotheses=[
            {"id": "H1", "status": "supported", "evidence": ["exp_1"]},
            {"id": "H2", "status": "active"},
        ])
    assert surfaced["status"] == "success"
    assert any("ready_candidate" in a and "H2" in a for a in surfaced["advisories"])

    ok = await _update_research_state(
        state, verdict="ready_candidate", change_reason="全部裁决完毕",
        hypotheses=[
            {"id": "H1", "status": "supported", "evidence": ["exp_1"]},
            {"id": "H2", "status": "refuted", "evidence": ["exp_2"]},
        ])
    assert ok["status"] == "success"
    assert not any("ready_candidate" in a for a in ok.get("advisories", []))


@pytest.mark.asyncio
async def test_unknown_verdict_rejected(tmp_path):
    state = _state(tmp_path)
    result = await _update_research_state(
        state, verdict="looks_good", hypotheses=[{"id": "H1", "status": "active"}])
    assert result["status"] == "error"
    assert any("verdict" in p for p in result["problems"])


# ── 后门 ──────────────────────────────────────────────────────────────


def test_generic_save_artifact_cannot_mint_research_state(tmp_path):
    """否则所有门禁只要换个工具名就绕过去了。"""
    state = _state(tmp_path)
    with pytest.raises(PermissionError):
        state.save_artifact(
            "research_state", "v99", "# 我说过了",
            metadata={"version": 99, "verdict": "ready_candidate", "hypotheses": []})


# ── 读 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_reports_first_round_before_any_version(tmp_path):
    state = _state(tmp_path)
    result = await _read_research_state(state)
    assert result["status"] == "success" and result["exists"] is False


@pytest.mark.asyncio
async def test_read_returns_latest_and_specific_version(tmp_path):
    state = _state(tmp_path)
    await _update_research_state(
        state, verdict="continue", hypotheses=[{"id": "H1", "status": "active"}])
    await _update_research_state(
        state, verdict="pivot", change_reason="换体系",
        hypotheses=[{"id": "H1", "status": "inconclusive"}])

    latest = await _read_research_state(state)
    assert latest["version"] == 2 and latest["verdict"] == "pivot"
    assert latest["available_versions"] == [1, 2]

    first = await _read_research_state(state, version=1)
    assert first["verdict"] == "continue"

    missing = await _read_research_state(state, version=7)
    assert missing["status"] == "error"


# ── 纯函数穷举 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("verdict", VERDICTS)
def test_all_declared_verdicts_are_accepted(verdict):
    problems, _advisories = validate_update(
        parent_meta=None,
        hypotheses=[{"id": "H1", "status": "inconclusive"}],
        verdict=verdict,
        change_reason="",
        frozen_ids=set(),
    )
    assert problems == []


def test_empty_hypotheses_are_legal():
    """判决拆除批 3w（research_state.py:176 删，档一非空闸）：hypotheses 可以
    为空——与 2026-08-16「假设非必需」演进一致，空清单不再拒绝。"""
    problems, advisories = validate_update(
        parent_meta=None, hypotheses=[], verdict="continue",
        change_reason="", frozen_ids=set())
    assert problems == []


def test_duplicate_ids_rejected():
    problems, _advisories = validate_update(
        parent_meta=None,
        hypotheses=[{"id": "H1", "status": "active"}, {"id": "H1", "status": "active"}],
        verdict="continue", change_reason="", frozen_ids=set())
    assert any("重复" in p for p in problems)


# ── 产物形状（供 mechanical QC 读） ───────────────────────────────────


@pytest.mark.asyncio
async def test_metadata_carries_fields_the_quality_check_reads(tmp_path):
    state = _state(tmp_path)
    await _update_research_state(
        state, verdict="continue",
        hypotheses=[{"id": "H1", "status": "active"}],
        gaps=["缺 300K 基线"], next_steps=["跑三档冷却速率"])
    record = state.read_artifact("research_state__research_state")
    meta = record["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    for field in ("version", "verdict", "hypotheses"):
        assert meta.get(field), f"quality_check require_present 读的是 {field}"
    assert "Research State v1" in record["content"]


# ── mechanical QC: require_present ────────────────────────────────────


def _check(paths, missing_as_false=True):
    return {
        "name": "research_state_current",
        "dimension": "scientific",
        "mechanical": {
            "artifact_type": "research_state",
            "require_present": paths,
            "treat_missing_as_false": missing_as_false,
        },
    }


class _Ctx:
    def __init__(self, state):
        self.state = state


@pytest.mark.asyncio
async def test_finish_gate_blocks_when_frozen_prereg_has_no_new_state(tmp_path):
    from nodes.hypothesis.hooks import _research_state_before_finish

    state = _state(tmp_path)
    _freeze_prereg(state, ["H1"])
    state.hook_state["_research_state_baseline_version"] = 0

    blocked = await _research_state_before_finish(_Ctx(state))
    assert blocked is not None
    assert "收尾被拦下" in blocked[0].content

    await _update_research_state(
        state, verdict="continue", hypotheses=[{"id": "H1", "status": "active"}])
    assert await _research_state_before_finish(_Ctx(state)) is None


@pytest.mark.asyncio
async def test_finish_gate_silent_before_any_freeze(tmp_path):
    """还没冻结协议 = 还没有科学产出，不该拦。"""
    from nodes.hypothesis.hooks import _research_state_before_finish

    state = _state(tmp_path)
    assert await _research_state_before_finish(_Ctx(state)) is None


@pytest.mark.asyncio
async def test_finish_gate_requires_a_NEW_version_this_round(tmp_path):
    """已有 v1 但本轮没更新 → 仍要拦。否则第二轮可以什么都不做就收尾。"""
    from nodes.hypothesis.hooks import _research_state_before_finish

    state = _state(tmp_path)
    _freeze_prereg(state, ["H1"])
    await _update_research_state(
        state, verdict="continue", hypotheses=[{"id": "H1", "status": "active"}])
    state.hook_state["_research_state_baseline_version"] = 1

    blocked = await _research_state_before_finish(_Ctx(state))
    assert blocked is not None and "收尾被拦下" in blocked[0].content


def test_round_briefing_says_first_round_then_next_round(tmp_path):
    from nodes.hypothesis.hooks import _analysis_round_briefing

    state = _state(tmp_path)
    first = _analysis_round_briefing(_Ctx(state))
    assert first is not None and "第 1 轮" in first[0].content
    # 同一 state 只注入一次
    assert _analysis_round_briefing(_Ctx(state)) is None

# ↓ 部分测试已随 #627（QC 判定层整体退场，core/quality_checks.py 删除）移除：
#   它们的被测对象是该层本身（_try_mechanical_fastpath）。层删了测试跟着走。
#   这批 import 断裂曾把整个 collection 挡住 —— 两个各自全绿的 PR 合并后互咬。
