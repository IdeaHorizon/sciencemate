"""critique id 的空值形态 + 账本反向恢复（issue #268，lujy 实测）。

现场：每个子节点跑完，decision package 都显示
    ⚠️ review 失败/不可用: null
    → fail-closed：默认推荐 REVISE
    [1] RETRY REVIEWER ← recommended   （菜单不含 PROCEED）
但 literature 明明产出了 survey_report、curator 整合完（12 concept + 12 claim）、
reviewer 也给了 critique，账本里 review_state=done —— 用户只能反复重试审稿。

两个缺口（都是我之前修的"一半"）：
  1. #208 只把 `review_failed_reason` 做了 "null"/"none"/"N/A" 归一化，
     **`review_critique_artifact_id` 没做** —— 字符串 "null" 是 truthy，
     拿去 read_artifact 读不到 → 判 review 不可用。
  2. #202 只做了"账本说没过 → 推翻传参"一个方向；**反方向没人管** ——
     账本说过了、传参却没给（或给了空值形态），合格 review 照样被判不可用。
     账本是权威，两个方向都该以它为准。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from core.artifact_provenance import forwarded, produced
from core.state import State
from shared.tools.library.decision_package import (
    _normalize_absent,
    _present_decision_package,
)


def _setup(review_state="done") -> State:
    st = State.new(node_type="_orchestrator", base_dir=Path(tempfile.mkdtemp()),
                   project_id="p1")
    # 真实回填形态：_import_required_outputs 写 produced_by_node_type=子 run 节点
    st.save_artifact(
        "review_critique", "lit_c",
        json.dumps({"verdict": "approve",
                    "recommended_action": {"action": "proceed"},
                    "concerns": []}),
        metadata={"produced_by_node_type": "_reviewer",
                  "source_node_type": "literature"},   # 被审对象来自 literature
        provenance=forwarded(
            produced("_reviewer", "run-review-1"),
            via_node_type=st.node_type,
            via_run_id=st.run_id,
        ))
    st.hook_state["pending_post_node_flow"] = [{
        "producing_node": "literature", "producing_run_id": "run-lit-1",
        "artifact_ids": ["survey_report__x"], "review_state": review_state,
        "review_critique_artifact_id": "review_critique__lit_c",
        "curator_state": "done", "decision_state": "pending"}]
    return st


def _present(st, cid, reason=None):
    asyncio.run(_present_decision_package(
        st, source_node_type="literature", producing_run_id="run-lit-1",
        review_critique_artifact_id=cid, review_failed_reason=reason))
    return st.hook_state["pending_post_node_flow"][0]["decision_options"]


@pytest.mark.parametrize("cid,reason", [
    ("null", "null"),          # #268 现场形态
    ("none", "N/A"),
    ("", ""),
    (None, None),              # 干脆没传
    ("review_critique__lit_c", None),   # 正常传（对照，不能被误伤）
])
def test_proceed_available_when_ledger_says_done(cid, reason):
    """账本 review_state=done + 有合格 critique → 无论传参是什么空值形态，
    菜单都必须给 PROCEED。"""
    opts = _present(_setup(), cid, reason)
    assert "proceed" in opts, f"cid={cid!r} reason={reason!r} 时 PROCEED 消失了"


def test_ledger_recovery_leaves_audit_event():
    st = _setup()
    _present(st, "null", "null")
    evs = [json.loads(x) for x in
           st.transcript_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [e for e in evs
            if e.get("event") == "review_critique_recovered_from_ledger"]


def test_normalize_covers_critique_id_forms():
    for v in ("null", "none", "N/A", "", "-", "undefined", None):
        assert _normalize_absent(v) is None
    assert _normalize_absent("review_critique__x") == "review_critique__x"


def test_ledger_not_done_still_fail_closed():
    """反向不能被这次修复破坏：账本说 review 没过 → 仍旧不给 PROCEED
    （#202 的保证，防止我这次"从账本恢复"把门开歪）。"""
    st = _setup(review_state="failed_awaiting_human")
    st.hook_state["pending_post_node_flow"][0]["review_critique_artifact_id"] = None
    opts = _present(st, "review_critique__lit_c", None)
    assert "proceed" not in opts
