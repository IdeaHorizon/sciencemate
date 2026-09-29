"""决策包呈的 critique 必须是 reviewer 这一轮真产出的那份。

2026-09-01 本机 E2E 实拍（最严重的一次 fail-open）：

  reviewer 本轮真产出  review_critique__..._v8   verdict=major_concerns
                                                 n_critical_concerns=1
                                                 recommended_action=revise
  决策包呈给人的       review_critique__...      verdict=approve  (0.88)
                       （8 月 30 日那份同名无版本后缀的基线）
  于是人看到           Verdict: approve
                       [1] PROCEED ← recommended

质量闸响了，人没看见，据此批了。它连 provenance 闸都过得去 —— 那份旧的也确实
是 _reviewer 写的。缺的从来不是「谁写的」，是「是不是这一次」。

框架其实早就知道答案：reviewer 子 run 结束时，run_node 从它**真实产出的
artifact** 上把 id 抄进了账本 entry。呈递时却去读调度器模型传进来的字符串。
框架自己答得出的事不该问模型。

夹具用没绑 worktree 的 State（run 本地账本）：critique 的登记时刻 `created_at`
在账本的 save 行上，"陈年"的那份就是带着旧登记时刻落账的那份。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

import pytest

from core.artifact_provenance import forwarded, produced, true_producer
from core.ledger import RecordStore
from core.state import State
from shared.tools.library.decision_package import _present_decision_package

# 本 run 开跑于此刻；run_id 前缀即纪元秒（core/state.py 的唯一生成处）。
NOW = int(time.time())
PRODUCING_RUN = f"{NOW}-9bff7b"


def _critique(verdict: str, action: str, concerns: list[dict]) -> str:
    return json.dumps({"verdict": verdict, "confidence": 0.85,
                       "recommended_action": {"action": action,
                                              "feedback_to_next_run": "见 concerns"},
                       "concerns": concerns})


def _save_critique(st: State, slug: str, verdict: str, action: str, concerns: list[dict],
                   *, created_at: str | None = None) -> str:
    """一份 reviewer 写的、经调度器转发的 critique。

    `created_at` 给出时直接在 run 本地账本上落一行带该登记时刻的 save —— 与
    `save_artifact` 同一个 RecordStore、同一套出处字段，只是登记时刻由夹具指定。
    """
    content = _critique(verdict, action, concerns)
    metadata = {"produced_by_node_type": "_reviewer", "source_node_type": "hypothesis"}
    provenance = forwarded(produced("_reviewer", f"run-review-{slug}"),
                           via_node_type=st.node_type, via_run_id=st.run_id)
    if created_at is None:
        return st.save_artifact("review_critique", slug, content,
                                metadata=metadata, provenance=provenance)["id"]
    by_node, by_run = true_producer({"provenance": provenance})
    store = RecordStore(st.root / "artifacts", st.root / "records.jsonl")
    record_id = f"review_critique__{slug}"
    store.save(
        artifact_id=record_id, artifact_type="review_critique", name=slug,
        content=content, metadata=metadata, directory=st.root / "artifacts",
        created_at=created_at, provenance=provenance,
        produced_by_node_type=by_node or "", produced_by_run_id=by_run or "",
        by_node=st.node_type, by_run=st.run_id,
    )
    return record_id


def _setup(*, ledger_cid: str | None, stale_created_at: str | None = None) -> State:
    st = State.new(node_type="_orchestrator", base_dir=Path(tempfile.mkdtemp()),
                   project_id="p1")
    # 陈年的那份：同名、无版本后缀、真是 reviewer 写的、说 approve
    _save_critique(st, "stale", "approve", "proceed", [], created_at=stale_created_at)
    # 本轮真产出：说 major_concerns，带一条 critical
    _save_critique(st, "v8", "major_concerns", "revise",
                   [{"severity": "critical",
                     "description": "Q3#3 在 experiment_log 明确标 not_run，"
                                    "不能靠改写承诺语义绕过。"}])
    st.hook_state["pending_post_node_flow"] = [{
        "producing_node": "hypothesis", "producing_run_id": PRODUCING_RUN,
        "artifact_ids": ["research_state__research_state"],
        "review_state": "done",
        "review_critique_artifact_id": ledger_cid,
        "curator_state": "done", "decision_state": "pending"}]
    return st


def _present(st: State, passed_cid: str | None) -> tuple[dict, str]:
    """返回 (账本 entry, 呈给人的决策包正文)。"""
    ret = asyncio.run(_present_decision_package(
        st, source_node_type="hypothesis", producing_run_id=PRODUCING_RUN,
        review_critique_artifact_id=passed_cid))
    shown = json.dumps(ret, ensure_ascii=False, default=str)
    return st.hook_state["pending_post_node_flow"][0], shown


def _events(st: State) -> list[dict]:
    return [json.loads(x) for x in
            st.transcript_path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_the_ledger_wins_when_the_caller_names_a_different_critique() -> None:
    """账本记着本轮真产出的那份 → 传参指向别的，一律不作数。"""
    st = _setup(ledger_cid="review_critique__v8")
    entry, shown = _present(st, "review_critique__stale")

    assert "Verdict: major_concerns" in shown, \
        "人看到的还是那份 approve —— 质量闸又被吞了"
    assert "Verdict: approve" not in shown
    assert "Q3#3 在 experiment_log 明确标 not_run" in shown, \
        "本轮那条 critical concern 没送到人眼前"
    assert entry["review_critique_artifact_id"] == "review_critique__v8"

    assert [e for e in _events(st)
            if e.get("event") == "review_critique_id_diverged_from_ledger"], \
        "换掉了传参却没留下审计事件"


def test_the_recommendation_follows_the_real_verdict() -> None:
    """真 verdict 是 major_concerns/revise → 不能推荐 PROCEED。"""
    st = _setup(ledger_cid="review_critique__v8")
    entry, shown = _present(st, "review_critique__stale")
    rec = str(entry.get("decision_recommended_action") or "")
    assert rec.startswith("revise"), \
        f"真 verdict 是 major_concerns/revise，推荐却是 {rec!r}"
    assert "Recommended: REVISE" in shown, \
        "正文里推荐的还是 PROCEED —— 人照样会点 1"


def test_matching_ids_are_not_disturbed() -> None:
    """传参与账本一致时不许有多余动作（免得审计事件失去信号价值）。"""
    st = _setup(ledger_cid="review_critique__v8")
    _present(st, "review_critique__v8")
    assert not [e for e in _events(st)
                if e.get("event") == "review_critique_id_diverged_from_ledger"]


def test_a_critique_older_than_the_run_cannot_be_its_review() -> None:
    """账本里没有这条 producing run 时的兜底：早于本 run 开跑的 critique
    不可能是对它的审查 —— 判据必须带起点，不能只问「这个 artifact 在不在」。"""
    # stale 的登记时刻在本 run 开跑之前
    st = _setup(ledger_cid=None, stale_created_at="2026-08-30T13:33:08.507529+00:00")
    st.hook_state["pending_post_node_flow"] = []          # 账本里没有这条

    entry_holder = st.hook_state.setdefault("pending_post_node_flow", [])
    asyncio.run(_present_decision_package(
        st, source_node_type="hypothesis", producing_run_id=PRODUCING_RUN,
        review_critique_artifact_id="review_critique__stale"))

    assert [e for e in _events(st)
            if e.get("event")
            == "review_critique_predates_the_run_it_claims_to_review"], \
        "陈年 critique 被当成了本轮审查"
    del entry_holder


def test_a_fresh_critique_is_not_flagged_as_stale() -> None:
    """反向：本轮写的 critique 不许被新鲜度闸误伤。"""
    st = _setup(ledger_cid=None)
    st.hook_state["pending_post_node_flow"] = []
    asyncio.run(_present_decision_package(
        st, source_node_type="hypothesis", producing_run_id=PRODUCING_RUN,
        review_critique_artifact_id="review_critique__v8"))
    assert not [e for e in _events(st)
                if e.get("event")
                == "review_critique_predates_the_run_it_claims_to_review"]
