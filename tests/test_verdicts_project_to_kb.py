"""已裁决的命题由框架投影成 KB claim（issue #748）。

## 现场（2026-08-30～09-01，项目 46da60b0）

| 事实 | 值 |
|---|---|
| `run.started` by nodeType | project_chat 19 / experiment 15 / _reviewer 13 / hypothesis 9 / literature 6 / **_curator 0** |
| research_state 里已裁决、带命题的问题 | Q1/Q2/Q3 supported、Q4 withdrawn |
| KB hypothesis claim | **0** |

claim 写入归 `_curator`（按需节点，不在 post-node flow 上）；调度器引导写着
"产出里有新的科学命题 → 值得起"；43 次它都判"不值得"。引导不是机制。

## 这里守什么

「Q1 被支持」是 research_state 里的机械事实，它的 KB 投影没有任何一步需要模型
判断。框架在 Analysis 收尾时自己做，走**同一条**写入路径 —— 账本该多严还多严：
承诺没兑现的照样降落 provisional。

夹具照真项目的形状造：预注册用 v0.5 的 `## Research Questions` + `proposition`
+ yaml 闭合条件（照 46da60b0 的 Q1 抄），research_state 用
`metadata.hypotheses: [{id, status, evidence, note}]`。冻结是账本上的一行
（`state.mark_frozen`），research_state 走它的生命周期写入口（typed-only）。
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from core.artifact_capabilities import save_typed_artifact
from core.bootstrap import bootstrap
from core.state import State
from shared.tools.library.kb import (
    _kb_register_artifact_as_chunk,
    project_research_state_to_kb,
)

bootstrap()

# 照真项目 46da60b0 的预注册形状：Q1 带命题（数值条 + 两条陈述条），Q2 不带命题。
PREREG = """
# 预注册：非正规性的增长伪造临界慢化早期预警信号

## Research Questions

### Q1: 在文献自带的统计尺度下，非正规性的增长是否系统性触发标准 EWS 报警？
- output_kind: 一条命题的裁决（真/假/存疑）
- proposition: 存在一族系统，其雅可比谱严格恒定于左半平面，但当非正规性随控制参数单调增长时，标准 CSD 预警指标随控制参数单调上升。
- falsification_criteria_structured:
  ```yaml
  - metric: kendall_tau
    comparison: ">"
    threshold: 0
  - statement: 报警率显著高于零假设基线（144 格点，α=0.05）。
  ```

### Q2: 严格假阳/真阳对照相图
- output_kind: 一张相图 + 可复现代码
- proposition: （无——本问题产出是一张相图，不是裁决一句话）
- 闭合条件:
  ```yaml
  - statement: 相图同时给出对照组与非正规组的 TPR 与 FPR。
  ```
"""


def _analysis_state(tmp_path, *, verdicts: list[dict], measured=None, discharges=None,
                    with_evidence: bool = True, project_id: str = "p748") -> State:
    """一个刚跑完的 Analysis run：自己产出了冻结预注册 + research_state，
    experiment 的证据产物在同一 State 可读（cross-node 读在真项目由 worktree 提供，
    这里用同一本 run 本地账本代替 —— 投影器只走 read_artifact / list_artifacts）。"""
    st = State.new(node_type="hypothesis", base_dir=tmp_path / "runs",
                   project_id=project_id)
    prereg_id = st.save_artifact("pre_registration", "ews", PREREG, {})["id"]
    st.mark_frozen(prereg_id)
    # 冻结即登记（freeze_artifact 对 pre_registration 的自动 chunk）
    asyncio.run(_kb_register_artifact_as_chunk(st, artifact_id=prereg_id))
    if with_evidence:
        log_id = st.save_artifact(
            "experiment_log", "q1", "results",
            {"measured_metrics": measured if measured is not None
             else {"kendall_tau": {"status": "measured", "value": 0.42}},
             "closure_discharges": discharges if discharges is not None
             else {"Q1#2": {"status": "discharged",
                            "evidence": "experiment_log__q1"}}})["id"]
        st.mark_frozen(log_id)
    # research_state 是 typed-only 产物（只有 Analysis 的生命周期工具能铸）：
    # 本 State 就是 Analysis，走它的生命周期写入口。
    save_typed_artifact(
        st, artifact_type="research_state", name="research_state",
        content="# Research State v3",
        metadata={"version": 3, "verdict": "ready_candidate", "hypotheses": verdicts},
    )
    return st


def _claims(st: State) -> list[dict]:
    return [c for c in st.list_kb("claims") if c.get("claim_type") == "hypothesis"]


def _events(st: State, name: str) -> list[dict]:
    if not st.transcript_path.exists():
        return []
    out = []
    for line in st.transcript_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            e = json.loads(line)
            if e.get("event") == name:
                out.append(e)
    return out


def _run(st: State) -> list[dict]:
    rs = st.read_artifact("research_state__research_state")
    return asyncio.run(project_research_state_to_kb(st, rs))


# ── 1. 主路径：supported → 一条 validated 的 hypothesis claim ────────────────


def test_supported_verdict_lands_as_a_validated_claim(tmp_path):
    st = _analysis_state(tmp_path, verdicts=[
        {"id": "Q1", "status": "supported", "evidence": ["experiment_log__q1"],
         "note": "报警率 mean=0.031、max=0.046，无一超过 α=0.05。"},
    ])
    assert _claims(st) == [], "前提：投影前 KB 里没有 hypothesis claim"

    out = _run(st)
    q1 = next(o for o in out if o["hypothesis_id"] == "Q1")
    assert q1["action"] == "flipped" and q1["landed_status"] == "validated", q1

    claims = _claims(st)
    assert len(claims) == 1
    c = claims[0]
    assert c["hypothesis_id"] == "Q1"
    assert c["status"] == "validated"
    assert c["claim_text"].startswith("存在一族系统")
    assert c["prereg_chunk_id"].startswith("chunk_")
    assert c["prereg_artifact_id"] == "pre_registration__ews"   # 结构化身份锚
    # 证据链：research_state 行里的 artifact 被登记成 chunk 并挂在翻转记录上
    flip = c["review_history"][-1]
    assert flip["to_status"] == "validated"
    assert flip["evidence_ids"] and all(e.startswith("chunk_") for e in flip["evidence_ids"])
    assert "research_state v3" in flip["reasoning"]
    # 事实进 transcript
    assert [e["action"] for e in _events(st, "kb_claim_projected")
            if e["hypothesis_id"] == "Q1"] == ["flipped"]


def test_questions_without_a_proposition_get_no_claim(tmp_path):
    """Q2 是相图不是命题 —— 没有 claim 可立，也不许硬立一条。"""
    st = _analysis_state(tmp_path, verdicts=[
        {"id": "Q1", "status": "supported", "evidence": ["experiment_log__q1"]},
        {"id": "Q2", "status": "supported", "evidence": ["experiment_log__q1"]},
    ])
    out = _run(st)
    assert next(o for o in out if o["hypothesis_id"] == "Q2")["action"] == "skipped"
    assert [c["hypothesis_id"] for c in _claims(st)] == ["Q1"]


# ── 2. 账永真：承诺没兑现 → 如实降落 provisional，不因为是框架写就放松 ─────


def test_unfulfilled_commitment_lands_provisional_not_validated(tmp_path):
    """Q1 的陈述条没勾除 —— research_state 说 supported 也不能进 KB 当 validated。"""
    st = _analysis_state(tmp_path, verdicts=[
        {"id": "Q1", "status": "supported", "evidence": ["experiment_log__q1"]},
    ], discharges={})                       # Q1#2 没有兑现记录
    out = _run(st)
    q1 = next(o for o in out if o["hypothesis_id"] == "Q1")
    assert q1["action"] == "flipped" and q1["landed_status"] == "provisional", q1
    assert "Q1#2" in q1["authority_note"]
    assert _claims(st)[0]["status"] == "provisional"


# ── 3. 不投影的裁决：withdrawn 不是知识；inconclusive 立而不翻 ─────────────


def test_withdrawn_is_not_projected_and_inconclusive_is_created_open(tmp_path):
    st = _analysis_state(tmp_path, verdicts=[
        {"id": "Q1", "status": "inconclusive", "evidence": ["experiment_log__q1"],
         "note": "证据不足以裁决"},
    ])
    out = _run(st)
    q1 = next(o for o in out if o["hypothesis_id"] == "Q1")
    assert q1["action"] == "created" and q1["landed_status"] == "open"

    # 另一个项目：KB 按 project_id 落盘，同一测试里两个 State 不能共用一份 KB
    st2 = _analysis_state(tmp_path / "b", project_id="p748b", verdicts=[
        {"id": "Q1", "status": "withdrawn", "evidence": []},
    ])
    out2 = _run(st2)
    assert next(o for o in out2 if o["hypothesis_id"] == "Q1")["action"] == "skipped"
    assert _claims(st2) == []


# ── 4. 幂等：Analysis 每次收尾都投影，不能翻两次、不能立两条 ────────────────


def test_projection_is_idempotent(tmp_path):
    st = _analysis_state(tmp_path, verdicts=[
        {"id": "Q1", "status": "supported", "evidence": ["experiment_log__q1"]},
    ])
    _run(st)
    again = _run(st)
    q1 = next(o for o in again if o["hypothesis_id"] == "Q1")
    assert q1["action"] == "existing" and q1["landed_status"] == "validated"
    claims = _claims(st)
    assert len(claims) == 1
    assert [h["to_status"] for h in claims[0]["review_history"]] == ["validated"]


# ── 5. 已有 curator 写的 claim：投影不造第二条，只对齐状态 ───────────────────


def test_existing_curator_claim_is_reused_not_duplicated(tmp_path):
    """真项目 46da60b0 后来由 curator 写了 Q1..Q3 —— 投影必须命中同一条身份。"""
    from shared.tools.library.kb import write_claim

    st = _analysis_state(tmp_path, verdicts=[
        {"id": "Q1", "status": "supported", "evidence": ["experiment_log__q1"]},
    ])
    prereg_chunk = next(c["id"] for c in st.list_kb("chunks")
                        if c.get("origin_artifact_id") == "pre_registration__ews")
    pre = asyncio.run(write_claim(
        st, claim_text="curator 写的措辞", claim_type="hypothesis",
        hypothesis_id="Q1", prereg_chunk_id=prereg_chunk,
        predicted_outcome="x", falsification_criteria_text="y",
        orphan_reason="test"))
    assert pre["status"] == "success"

    out = _run(st)
    q1 = next(o for o in out if o["hypothesis_id"] == "Q1")
    assert q1["claim_id"] == pre["id"]
    assert len(_claims(st)) == 1


# ── 6. 工具面：提案期假说照写，如实记 adoption=proposed；投影时翻成 adopted ──
#
# 判决拆除·第三波：「只许 _curator 写 hypothesis」是角色事前审批，删。账仍然
# 是真的：created_by_node_type + adoption 把「谁在什么身份下写」钉死；Analysis
# 收尾的机械投影仍是已裁决命题的权威来源，并把同一条提案期假说标成 adopted。


def test_model_facing_write_of_a_proposal_stage_hypothesis_is_marked_not_refused(tmp_path):
    from shared.tools.library.kb import _create_claim

    st = _analysis_state(tmp_path, verdicts=[])
    prereg_chunk = next(c["id"] for c in st.list_kb("chunks")
                        if c.get("origin_artifact_id") == "pre_registration__ews")
    res = asyncio.run(_create_claim(
        st, claim_text="提案期的假说", claim_type="hypothesis",
        hypothesis_id="Q1", prereg_chunk_id=prereg_chunk, sources=[prereg_chunk],
        predicted_outcome="x", falsification_criteria_text="y", orphan_reason="test"))
    assert res["status"] == "success", res          # 墙若加回来这条转红
    assert res["adoption"] == "proposed"
    rec = st.get_kb_record("claims", res["id"])
    assert rec["adoption"] == "proposed" and rec["created_by_node_type"] == "hypothesis"
    assert _events(st, "hypothesis_claim_written_before_adoption")


def test_projection_adopts_the_proposal_stage_hypothesis_in_place(tmp_path):
    from shared.tools.library.kb import _create_claim

    st = _analysis_state(tmp_path, verdicts=[
        {"id": "Q1", "status": "supported", "evidence": ["experiment_log__q1"]},
    ])
    prereg_chunk = next(c["id"] for c in st.list_kb("chunks")
                        if c.get("origin_artifact_id") == "pre_registration__ews")
    pre = asyncio.run(_create_claim(
        st, claim_text="节点写的措辞", claim_type="hypothesis",
        hypothesis_id="Q1", prereg_chunk_id=prereg_chunk, sources=[prereg_chunk],
        predicted_outcome="x", falsification_criteria_text="y", orphan_reason="test"))
    assert pre["status"] == "success" and pre["adoption"] == "proposed"

    out = _run(st)
    q1 = next(o for o in out if o["hypothesis_id"] == "Q1")
    assert q1["claim_id"] == pre["id"]
    assert q1["adoption"] == "adopted"
    assert st.get_kb_record("claims", pre["id"])["adoption"] == "adopted"
    assert len(_claims(st)) == 1


# ── 7. 接线：finalize_run 真的调它（查 Call，不查名字出现）────────────────────


def test_finalize_run_calls_the_projector():
    src = Path(__file__).resolve().parents[1] / "core" / "executor.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "finalize_run")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "project_research_state_to_kb"]
    assert calls, "finalize_run 里没有对 project_research_state_to_kb 的调用"
