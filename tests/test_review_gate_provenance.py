"""review 门的独立性必须是**机制**，不是约定（issue #202）。

qinp 2026-07-28 PODsys canonical E2E 实测：literature 的 5 次审稿全部失败
（blank-out / args 截断 ×3 / Markdown 非 JSON），orchestrator 随后自己调
save_artifact 写了一份 review_critique，review 门随即打开，菜单从 review-failed
专用集（无 PROCEED）切成普通集并推荐 [1] PROCEED。

三层缺口叠加：
  1. save_artifact 的 owner guard 只覆盖 producing 节点产出，review_critique
     是 _reviewer（架构节点）的产出 → 不在表内 → 自产畅通。对照：orchestrator
     写 manuscript 会被当场拒绝 —— 漏的恰恰是最该防的那个。
  2. 渲染选项集读**调用方传参**而非账本 review_state。
  3. proceed 分支不校验 review_state 就把 flow 整条出列 → 下游全放行。
本轮纯靠操作者没按 PROCEED 才没实际突破。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from core.artifact_capabilities import save_typed_artifact
from core.artifact_provenance import forwarded, produced
from core.state import State
from shared.tools.builtin import _save_artifact
from shared.tools.library.decision_package import (
    _NORMAL_ACTIONS,
    _REVIEW_FAILED_ACTIONS,
    _critique_from_metadata,
    _present_decision_package,
    critique_provenance_problem,
    record_decision_answer,
)

_CRITIQUE = json.dumps({
    "verdict": "approve_with_revisions",
    "concerns": [{"severity": "minor", "summary": "论文计数不符"}],
    "recommended_action": {"action": "proceed", "target_node": None},
}, ensure_ascii=False)


def _state(node_type="_orchestrator") -> State:
    st = State.new(node_type=node_type, base_dir=Path(tempfile.mkdtemp()),
                   project_id="p1")
    return st


def _trusted_review(state: State, name: str = "lit_critique") -> dict:
    original = state.node_type
    state.node_type = "_reviewer"
    try:
        return save_typed_artifact(
            state,
            artifact_type="review_critique",
            name=name,
            content=_CRITIQUE,
            metadata={},
        )
    finally:
        state.node_type = original


def _forwarded_review(
    state: State,
    *,
    producer: str,
    producer_run: str,
    metadata: dict | None = None,
    content: str = _CRITIQUE,
) -> dict:
    return state.save_artifact(
        "review_critique",
        "lit_critique",
        content,
        metadata=metadata or {},
        provenance=forwarded(
            produced(producer, producer_run),
            via_node_type=state.node_type,
            via_run_id=state.run_id,
        ),
    )


def _flow(state: State, *, review_state="failed_awaiting_human",
          critique_id=None, capped=False) -> dict:
    entry = {
        "producing_node": "literature",
        "producing_run_id": "run-lit-1",
        "artifact_ids": ["a1"],
        "review_state": review_state,
        "review_critique_artifact_id": critique_id,
        "review_attempt_count": 2 if capped else 0,
        "curator_state": "done",
        "decision_state": "pending",
    }
    if capped:
        entry["review_retry_capped"] = True
    state.hook_state["pending_post_node_flow"] = [entry]
    return entry


def _events(state: State, name: str) -> list[dict]:
    if not state.transcript_path.exists():
        return []
    out = []
    for line in state.transcript_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") == name:
            out.append(e)
    return out


# ── A. 自产 critique 不解除门禁 ──────────────────────────────────────────────


def test_orchestrator_cannot_create_review_critique():
    """核心：被审查方/编排方不能凭空新建 review_critique（写 manuscript 早就
    被拦，review_critique 之前却是敞开的）。"""
    st = _state("_orchestrator")
    r = asyncio.run(_save_artifact(st, "review_critique", "lit_critique", _CRITIQUE))
    assert r["status"] == "error"
    assert "_reviewer" in r["error"]


def test_reviewer_can_create_and_gets_provenance_stamp():
    """合格 reviewer 正常产出，且被盖上 provenance 章。"""
    st = _state("_reviewer")
    _trusted_review(st)
    rec = st.read_artifact("review_critique__lit_critique")
    assert rec["produced_by_node_type"] == "_reviewer"     # 框架章在 record 顶层
    assert rec["produced_by_run_id"] == st.run_id


def test_provenance_problem_detects_non_reviewer():
    assert critique_provenance_problem(
        {"metadata": {"produced_by_node_type": "_orchestrator"}}) is not None
    assert critique_provenance_problem(
        {"metadata": {"produced_by_node_type": "_reviewer"}}) is None


def test_provenance_missing_is_allowed_for_legacy():
    """本改动之前存的 artifact 没有章 —— 硬拦会把历史 run 全判死。
    新写入一律带章，真实洞照样堵死。"""
    assert critique_provenance_problem({"metadata": {}}) is None
    assert critique_provenance_problem(None) is None


def test_self_authored_critique_does_not_open_gate():
    """A（端到端，复刻事故现场）：orchestrator 自产 critique + 账本记着 review
    失败 → 门必须保持关闭。两层防线（账本优先 / provenance）任一拦下都算。"""
    st = _state("_orchestrator")
    _forwarded_review(
        st, producer="_orchestrator", producer_run=st.run_id
    )
    entry = _flow(st)
    asyncio.run(_present_decision_package(
        st, source_node_type="literature", producing_run_id="run-lit-1",
        review_critique_artifact_id="review_critique__lit_critique"))

    assert entry["decision_options"] == _REVIEW_FAILED_ACTIONS
    assert "proceed" not in entry["decision_options"]
    assert entry["review_state"] == "failed_awaiting_human"
    assert (_events(st, "review_critique_ledger_override")
            or _events(st, "review_critique_provenance_rejected"))


def test_provenance_layer_alone_blocks_self_authored():
    """provenance 层**单独**有效：账本即便记着 review_state=done 并指向这份
    artifact，只要它是被审查方自己写的，仍不得解除门禁。

    这条是真正的独立性保证 —— 前一条里账本先拦下了，掩盖了本层是否工作。
    """
    st = _state("_orchestrator")
    _forwarded_review(
        st, producer="_orchestrator", producer_run=st.run_id
    )
    entry = _flow(st, review_state="done",
                  critique_id="review_critique__lit_critique")
    asyncio.run(_present_decision_package(
        st, source_node_type="literature", producing_run_id="run-lit-1",
        review_critique_artifact_id="review_critique__lit_critique"))

    assert entry["decision_options"] == _REVIEW_FAILED_ACTIONS
    assert "proceed" not in entry["decision_options"]
    ev = _events(st, "review_critique_provenance_rejected")
    assert ev and "_orchestrator" in ev[-1]["reason"]


# ── A2. 账本优先于传参 ──────────────────────────────────────────────────────


def test_ledger_overrides_passed_critique_id():
    """账本说 review 没过（critique_id=null），传参塞了个合法 critique 也不作数。"""
    st = _state("_orchestrator")
    rv = _state("_reviewer")
    # 合格 reviewer 产出的 critique，但账本里这轮 review 记的是失败
    _trusted_review(rv)
    # 模拟真实回填：_import_required_outputs 强制写 produced_by_node_type。
    # 注意 source_node_type 是 reviewer 契约里"**被审查对象**的来源"（literature），
    # 含义与产出方相反 —— 一并写上，锁死"不能拿它当 provenance"。
    _forwarded_review(
        st,
        producer="_reviewer",
        producer_run=rv.run_id,
        metadata={"source_node_type": "literature"},
    )
    entry = _flow(st, review_state="failed_awaiting_human", critique_id=None)
    asyncio.run(_present_decision_package(
        st, source_node_type="literature", producing_run_id="run-lit-1",
        review_critique_artifact_id="review_critique__lit_critique"))

    assert entry["decision_options"] == _REVIEW_FAILED_ACTIONS
    assert _events(st, "review_critique_ledger_override")


def test_proceed_rejected_when_review_not_done():
    """A2 后半：直接投喂 proceed → 拒绝、flow **不出列**、下游仍被拦。"""
    st = _state("_orchestrator")
    entry = _flow(st, review_state="failed_awaiting_human")
    entry["decision_options"] = list(_NORMAL_ACTIONS)      # 假装菜单给了 PROCEED

    record_decision_answer(st, {"type": "decision_package",
                                "producing_run_id": "run-lit-1"}, "1")

    flow = st.hook_state["pending_post_node_flow"]
    assert len(flow) == 1                       # 没出列
    assert flow[0]["review_state"] == "failed_awaiting_human"
    assert flow[0]["decision_state"] == "awaiting_human"
    assert flow[0].get("accepted_action") is None
    ev = _events(st, "decision_action_rejected")
    assert ev and ev[-1]["reason"] == "review_state_not_done"
    assert ev[-1]["flow_closed"] is False


# ── B. 正确路径不回归 ───────────────────────────────────────────────────────


def test_reviewer_produced_critique_opens_gate():
    """同一份 critique 由 _reviewer 产出 + 账本 done → 菜单有 PROCEED。"""
    st = _state("_orchestrator")
    rv = _state("_reviewer")
    # 模拟真实回填：_import_required_outputs 强制写 produced_by_node_type。
    # 注意 source_node_type 是 reviewer 契约里"**被审查对象**的来源"（literature），
    # 含义与产出方相反 —— 一并写上，锁死"不能拿它当 provenance"。
    _forwarded_review(
        st,
        producer="_reviewer",
        producer_run=rv.run_id,
        metadata={"source_node_type": "literature"},
    )
    entry = _flow(st, review_state="done",
                  critique_id="review_critique__lit_critique")
    asyncio.run(_present_decision_package(
        st, source_node_type="literature", producing_run_id="run-lit-1",
        review_critique_artifact_id="review_critique__lit_critique"))

    assert entry["decision_options"] == _NORMAL_ACTIONS
    assert "proceed" in entry["decision_options"]
    assert not _events(st, "review_critique_provenance_rejected")


def test_proceed_allowed_when_review_done():
    st = _state("_orchestrator")
    entry = _flow(st, review_state="done", critique_id="c1")
    entry["decision_options"] = list(_NORMAL_ACTIONS)
    record_decision_answer(st, {"type": "decision_package",
                                "producing_run_id": "run-lit-1"}, "1")
    assert st.hook_state["pending_post_node_flow"] == []      # 正常出列
    assert entry["accepted_action"] == "proceed"


def test_skipped_review_still_proceeds():
    """owner 显式 opt-out review（skip_post_node_review）→ 不该被新门禁误伤。"""
    st = _state("_orchestrator")
    entry = _flow(st, review_state="skipped")
    entry["decision_options"] = list(_NORMAL_ACTIONS)
    record_decision_answer(st, {"type": "decision_package",
                                "producing_run_id": "run-lit-1"}, "1")
    assert st.hook_state["pending_post_node_flow"] == []


# ── 封顶不死锁（#202 × #208 交汇，我加的额外防线）─────────────────────────


def test_capped_retry_restores_proceed_option():
    """reviewer 重试封顶（#208）后必须把 PROCEED 放回菜单 —— 否则
    "无 PROCEED 的选项集 + 重试上限" = 人工无路可走（我 #151 犯过的错）。"""
    st = _state("_orchestrator")
    entry = _flow(st, review_state="failed_awaiting_human", capped=True)
    asyncio.run(_present_decision_package(
        st, source_node_type="literature", producing_run_id="run-lit-1",
        review_failed_reason="reviewer 连续失败"))
    assert "proceed" in entry["decision_options"]


def test_capped_proceed_recorded_as_override_not_pass():
    """封顶后人工选 PROCEED = 知情 override：允许放行，但**绝不**记成
    "review 通过"，并留下醒目审计事件。"""
    st = _state("_orchestrator")
    entry = _flow(st, review_state="failed_awaiting_human", capped=True)
    entry["decision_options"] = list(_NORMAL_ACTIONS)

    record_decision_answer(st, {"type": "decision_package",
                                "producing_run_id": "run-lit-1"}, "1")

    assert entry["review_state"] == "human_override_without_review"
    assert entry["review_state"] != "done"          # 没有伪装成通过
    ev = _events(st, "review_gate_human_override")
    assert ev and "未经有效独立审查" in ev[-1]["note"]


# ── D. metadata 回退 ────────────────────────────────────────────────────────


def test_metadata_fallback_builds_usable_critique():
    """reviewer 契约强制要求 metadata 写 verdict/recommended_action
    "让 decision package 能直接读"，但消费方一直只认 content。"""
    out = _critique_from_metadata({"metadata": {
        "verdict": "approve_with_revisions",
        "recommended_action": "proceed",
        "n_concerns": 3,
    }})
    assert out["verdict"] == "approve_with_revisions"
    assert out["recommended_action"]["action"] == "proceed"
    assert out["_source"] == "metadata_fallback"
    assert len(out["concerns"]) == 1          # 有计数无明细 → 占位，不伪造内容


def test_metadata_fallback_returns_none_without_key_fields():
    assert _critique_from_metadata({"metadata": {"n_concerns": 3}}) is None
    assert _critique_from_metadata({"metadata": {}}) is None
    assert _critique_from_metadata(None) is None


def test_markdown_critique_recovered_via_metadata():
    """实测场景：reviewer#4 把 critique 写成 Markdown，metadata 完全合规，
    却被整份判为不可用。现在应能靠 metadata 走通。"""
    st = _state("_orchestrator")
    rv = _state("_reviewer")
    _forwarded_review(
        st,
        producer="_reviewer",
        producer_run=rv.run_id,
        content="## Verdict\napprove_with_revisions\n\n## Concerns\n- minor: 计数不符",
        metadata={
            "source_node_type": "literature",
            "verdict": "approve_with_revisions",
            "recommended_action": "proceed",
            "n_concerns": 3,
        },
    )
    entry = _flow(st, review_state="done",
                  critique_id="review_critique__lit_critique")
    asyncio.run(_present_decision_package(
        st, source_node_type="literature", producing_run_id="run-lit-1",
        review_critique_artifact_id="review_critique__lit_critique"))

    assert entry["decision_options"] == _NORMAL_ACTIONS     # 没被判成 review 失败
    assert _events(st, "review_critique_metadata_fallback")


# ── 回归：source_node_type 键名撞车（2026-07-30 实测事故）──────────────────


def test_source_node_type_is_not_provenance():
    """`source_node_type` 在两处含义**相反**，绝不能拿它判 provenance。

    - `_import_required_outputs`：产出该 artifact 的子节点
    - `_reviewer` harness 契约：review_critique 里它指**被审查对象**的来源节点
      （审 literature 的产物就写 literature）
    setdefault 保留 reviewer 先写的那个 → 导入后 source_node_type='literature'。

    PR#209 曾第一优先读它，导致 5 次成功审稿（全 completed + approve）被判成
    "由 literature 产出、不是独立 reviewer"，强制 retry_reviewer 白烧 token。
    """
    legit = {
        "metadata": {
            "source_node_type": "literature",      # 契约：被审对象的来源
            "produced_by_node_type": "_reviewer",  # 框架：真实产出方
        },
        "produced_by_node_type": "_orchestrator",  # 回填走 parent 的 save_artifact
    }
    assert critique_provenance_problem(legit) is None

    # 反向：真由 literature 自己写的 critique 仍必须拦下
    forged = {"metadata": {"source_node_type": "literature",
                           "produced_by_node_type": "literature"},
              "produced_by_node_type": "_orchestrator"}
    assert critique_provenance_problem(forged) is not None


def test_import_stamps_framework_owned_provenance():
    """`_import_required_outputs` 必须**强制**写 produced_by_*（不是 setdefault）
    —— 节点不能通过先占字段来伪造产出方。"""
    import inspect

    from shared.tools import run_node
    src = inspect.getsource(run_node._import_required_outputs)
    assert 'meta["produced_by_node_type"] = child_summary["node_type"]' in src
    assert 'meta["produced_by_run_id"] = child_summary["run_id"]' in src
