"""效果传感器：org 卡送出去之后发生了什么。

## 它替换掉了什么

`find_stale_suspects` 原来读 `last_cited_at` —— **全仓没有任何地方写这个字段**。
于是老化复查永远落回 `created_at`：一张天天被引用的卡和一张没人看的卡，
老化得一样快。这是「从不更新的字段不是事实」的又一个病例。

引用改成**机械推导**（project claim 的 sources 里出现 org id），不新开写路径：
只要还需要模型主动上报，它就有一半概率不报。
"""
from __future__ import annotations

import inspect

import pytest

from core import org_dreaming, org_usage
from core.bootstrap import bootstrap
from core.state import State

bootstrap()
_AT = "2026-08-21T00:00:00Z"


def _state(tmp_path, pid="p_usage") -> State:
    return State.new(node_type="_curator", base_dir=tmp_path, project_id=pid)


def _org_card(st: State, text: str) -> str:
    rec, _ = st.write_kb("claims", {
        "claim_text": text, "statement": text, "claim_type": "empirical",
        "org_kind": "verified_finding", "domain": "cond-mat.stat-mech",
        "concept_ids": [], "orphan_reason": "stub", "scope": "org",
        "sources": ["doi:10/x"], "confidence": 0.8,
        "promoted_from": {"project_id": "p_src", "source_id": "claim_" + "a" * 12,
                          "approved_by": "wangd", "at": _AT}})
    return rec["id"]


def test_injection_is_recorded(tmp_path):
    st = _state(tmp_path)
    oid = _org_card(st, "一条结论")
    org_usage.record_injection(st, [oid], project_id="p_new", at=_AT)
    org_usage.record_injection(st, [oid], project_id="p_other", at=_AT)

    u = org_usage.usage_of(st, oid)
    assert u.injected == 2
    assert set(u.injected_into) == {"p_new", "p_other"}
    assert u.cited == 0
    assert u.sent_but_ignored is True
    assert u.never_sent is False


def test_citation_is_derived_not_reported(tmp_path):
    """引用靠机械推导，不靠谁记得上报。"""
    st = _state(tmp_path)
    oid = _org_card(st, "一条被下游用上的结论")
    st.write_kb("claims", {
        "claim_text": "新项目基于该 org 结论的观察", "claim_type": "empirical",
        "scope": "project", "concept_ids": [], "orphan_reason": "stub",
        "sources": [oid], "confidence": 0.7, "created_at": _AT})

    u = org_usage.usage_of(st, oid)
    assert u.cited == 1
    assert u.last_cited_at == _AT
    assert u.sent_but_ignored is False


def test_never_sent_and_sent_but_ignored_are_different_things(tmp_path):
    st = _state(tmp_path)
    dormant = _org_card(st, "一条从没被送出去的结论")
    ignored = _org_card(st, "一条送出去过但没人用的结论")
    org_usage.record_injection(st, [ignored], project_id="p_new", at=_AT)

    report = org_usage.usage_report(st)
    assert dormant not in report or report[dormant].never_sent
    assert report[ignored].sent_but_ignored is True
    assert report[ignored].never_sent is False


def test_sensor_failure_never_breaks_the_read_path(tmp_path, monkeypatch):
    """观测设施把被观测的过程搞挂，是最坏的那种耦合。"""
    st = _state(tmp_path)
    monkeypatch.setattr(org_usage, "_ledger_path",
                        lambda: (_ for _ in ()).throw(OSError("盘坏了")))
    org_usage.record_injection(st, ["claim_x"], project_id="p", at=_AT)  # 不抛
    assert org_usage.usage_report(st) == {} or True


def test_ledger_read_is_bounded(tmp_path):
    """输出是聚合计数，不是原始账本 —— 与账本长度无关。"""
    st = _state(tmp_path)
    oid = _org_card(st, "一条被反复注入的结论")
    for i in range(200):
        org_usage.record_injection(st, [oid], project_id=f"p{i}", at=_AT)
    report = org_usage.usage_report(st)
    assert len(report) == 1
    assert report[oid].injected == 200


def test_single_event_id_list_is_capped(tmp_path):
    st = _state(tmp_path)
    org_usage.record_injection(st, [f"claim_{i:012d}" for i in range(500)],
                               project_id="p", at=_AT)
    assert sum(len(e.get("org_ids") or ()) for e in org_usage._read_ledger()) \
        <= org_usage.MAX_IDS_PER_EVENT


def test_aging_no_longer_reads_a_field_nobody_writes():
    """墓碑：`last_cited_at`。

    判据要问「谁维护这行」，不问「值像不像活的」。
    """
    src = inspect.getsource(org_dreaming.find_stale_suspects)
    assert 'rec.get("last_cited_at")' not in src
    assert "usage_report" in src
