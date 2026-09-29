"""org 知识的送达 —— 沉淀只是回路的一半。

判据全是行为：造 org 盘面 → 调注入/红旗/书目 → 看送达内容。
核心不变量：**输出量与 org 总量无关**（有界读取原则）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from core import org_delivery as od
from core.state import State

_PROV = {"project_id": "p_prev", "source_id": "src", "approved_by": "wangd",
         "at": "2026-08-21T00:00:00Z"}


def _state() -> State:
    return State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()),
                     project_id="p_new")


def _org_finding(state: State, statement: str, *, domain: str | None = None,
                 **extra) -> dict:
    rec, _ = state.write_kb("claims", {
        "claim_text": statement, "statement": statement,
        "claim_type": "empirical", "org_kind": "verified_finding",
        "concept_ids": ["c"], "sources": ["doi:10/x"],
        "scope": "org", "promoted_from": dict(_PROV),
        **({"domain": domain} if domain else {}), **extra})
    return rec


def _org_dead_end(state: State, trigger: str, warning: str, **extra) -> dict:
    rec, _ = state.write_kb("claims", {
        "claim_text": warning, "claim_type": "dead_end",
        "org_kind": "dead_end", "trigger": trigger, "warning": warning,
        "concept_ids": ["c"], "sources": ["doi:10/x"],
        "dont_repeat_reason": warning,
        "scope": "org", "promoted_from": dict(_PROV), **extra})
    return rec


# ── 开题注入 ────────────────────────────────────────────────────────────────


def test_empty_org_injects_nothing():
    """org 没东西时不编造 —— "本组没读过这个方向"本身是信息。"""
    assert od.org_orientation(_state()) is None


def test_orientation_carries_findings_and_dead_ends():
    st = _state()
    _org_finding(st, "通用 MLIP 在 >30GPa 重构型相变区误差显著偏大",
                 applicability={"regime": "P>30GPa"},
                 confidence_basis="两个项目实测", replication_count=2)
    _org_dead_end(st, "相变点附近 NPT 平衡 Berendsen",
                  "会产生假中间相，用 Parrinello-Rahman",
                  cost_when_hit={"wall_days": 3})

    text = od.org_orientation(st)
    assert text and od.ORG_ORIENTATION_PREFIX in text
    assert "重构型相变区误差" in text
    assert "假中间相" in text
    assert "跨项目复现 2 次" in text, "复现记数是校准信号，必须送到"
    assert "P>30GPa" in text, "适用条件必须随结论一起送到"
    assert "3" in text, "死路的代价要说出来"


def test_orientation_output_is_bounded_by_constants():
    """输出量与 org 总量无关 —— 三年后 org 一万条，注入量还是这些。"""
    st = _state()
    for i in range(60):
        _org_finding(st, f"结论 {i}：某条件下的系统性偏差 {i}")
    for i in range(40):
        _org_dead_end(st, f"触发条件 {i}", f"死路警告 {i}")

    text = od.org_orientation(st)
    assert text.count("  • ") <= od.INJECT_MAX_FINDINGS + od.INJECT_MAX_DEAD_ENDS


def test_project_scope_records_never_leak_into_org_injection():
    """注入的是**组织资产**，别把本项目自己的工作记忆当"已知"喂回去。"""
    st = _state()
    st.write_kb("claims", {
        "claim_text": "本项目的中间断言", "claim_type": "empirical",
        "concept_ids": ["c"], "sources": ["doi:10/y"]})
    assert od.org_orientation(st) is None


# ── reviewer 红旗 ───────────────────────────────────────────────────────────


def test_dead_end_flag_fires_on_a_matching_plan():
    st = _state()
    _org_dead_end(st, "Berendsen barostat NPT",
                  "相变点附近会产生假中间相；改用 Parrinello-Rahman")
    flags = od.dead_end_flags(
        st, "我们将用 NPT 系综配 Berendsen barostat 在相变点附近平衡")
    assert len(flags) == 1
    assert "Parrinello-Rahman" in flags[0]["warning"]
    assert flags[0]["from_project"] == "p_prev", "红旗要说清是谁踩过"


def test_dead_end_flag_is_conservative():
    """误报会让人学会忽略红旗 —— 那比没有红旗更糟，所以判据保守。"""
    st = _state()
    _org_dead_end(st, "Berendsen barostat NPT", "假中间相")
    assert od.dead_end_flags(st, "我们用 Nose-Hoover 恒温器做 NVT 平衡") == []
    assert od.dead_end_flags(st, "") == []


def test_dead_ends_without_trigger_cannot_flag():
    """没有触发条件的死路警告发不出红旗 —— 这正是晋升时要补 trigger 的原因。"""
    st = _state()
    st.write_kb("claims", {
        "claim_text": "某个失败", "claim_type": "dead_end", "org_kind": "dead_end",
        "concept_ids": ["c"], "sources": ["doi:10/x"],
        "dont_repeat_reason": "失败了", "scope": "org",
        "promoted_from": dict(_PROV)})
    assert od.dead_end_flags(st, "任何计划正文") == []


# ── 书目复用 ────────────────────────────────────────────────────────────────


def test_biblio_hits_return_group_readings():
    """读过的论文直接取"本组读后结论"，预算全花在新论文上。"""
    st = _state()
    st.write_kb("chunks", {
        "text": "该文报告 30GPa 以上偏差增大", "source": "doi:10.1038/abc",
        "scope": "org", "org_kind": "biblio",
        "group_readings": [{"project_id": "p_prev", "at": "2026-08-01"}],
        "promoted_from": dict(_PROV)})

    hits = od.biblio_hits(st, ("doi:10.1038/abc", "doi:10.9999/unread"))
    assert set(hits) == {"doi:10.1038/abc"}
    assert hits["doi:10.1038/abc"]["group_readings"][0]["project_id"] == "p_prev"


def test_biblio_hits_ignore_project_scope_chunks():
    st = _state()
    st.write_kb("chunks", {"text": "本项目选段", "source": "doi:10.1038/abc"})
    assert od.biblio_hits(st, ("doi:10.1038/abc",)) == {}


def test_biblio_hits_with_no_anchors_is_empty():
    assert od.biblio_hits(_state(), ()) == {}


# ── 接线：机制存在 ≠ 接到路径 ───────────────────────────────────────────────


def _hook():
    import core.loop_hooks_builtin  # noqa: F401  注册副作用
    from core.loop_hooks import get_loop_hook

    return get_loop_hook("org_orientation")


def test_org_orientation_is_registered_as_a_hook():
    """光有 org_delivery 这个模块不算送达 —— 必须有人机械调它。

    「机制存在但没接到路径」是本仓反复付过代价的形状：声明了一条管道、
    一步都没接，两边都不报错。
    """
    hook = _hook()
    assert hook is not None and hook.on_turn_start is not None


def test_hook_injects_for_question_forming_nodes():
    from types import SimpleNamespace

    st = _state()          # node_type="hypothesis"
    _org_finding(st, "通用方法在该区间外推误差偏大")

    out = _hook().on_turn_start(SimpleNamespace(state=st, turn=1))
    assert out and od.ORG_ORIENTATION_PREFIX in out[0].content


def test_hook_injects_once_per_state():
    from types import SimpleNamespace

    st = _state()
    _org_finding(st, "某条结论")
    ctx = SimpleNamespace(state=st, turn=1)
    assert _hook().on_turn_start(ctx)
    assert _hook().on_turn_start(ctx) is None, "重复注入是纯浪费"


def test_hook_is_silent_when_org_is_empty():
    from types import SimpleNamespace

    st = _state()
    assert _hook().on_turn_start(SimpleNamespace(state=st, turn=1)) is None


# ── 正典优先：新项目该读综述，不该读 400 条原子 claim ───────────────────────


def test_canon_leads_the_injection_and_absorbed_cards_step_aside():
    """成熟 org 主要通过活综述被阅读；被吸收的卡片让出注入面的位置。

    **让出 ≠ 删除** —— 它们还在账本里，只是那个位置该留给综述没讲到的新东西。
    """
    from core import org_canon as canon

    st = _state()
    absorbed = _org_finding(st, "已被综述吸收的一条结论", domain="mlip")
    fresh = _org_finding(st, "综述之后新晋升的一条结论", domain="mlip")
    canon.write_canon(st, domain="mlip",
                      body="# MLIP 高压可靠性\n\n本域共识：……",
                      absorbed_ids=[absorbed["id"]], at=_PROV["at"])

    text = od.org_orientation(st, domain="mlip")
    assert "领域活综述" in text and "本域共识" in text
    assert "综述之后新晋升" in text, "综述没讲到的新东西必须还在"
    assert "已被综述吸收" not in text, "吸收过的不该再占注入面"
    assert st.get_kb_record("claims", absorbed["id"]) is not None, "但账本里必须还在"


def test_without_a_canon_cards_still_carry_the_injection():
    """冷启动阶段还没有综述 —— 那就直接送卡片，不能因此什么都不送。"""
    st = _state()
    _org_finding(st, "冷启动期的一条结论", domain="mlip")
    text = od.org_orientation(st, domain="mlip")
    assert text and "冷启动期的一条结论" in text
    assert "领域活综述" not in text
