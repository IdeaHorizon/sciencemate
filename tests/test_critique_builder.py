"""compose_review_critique：review_critique 增量拼装落盘（#184/#202 建议 5）。

背景：整份 critique JSON（实测 6-8KB）塞 save_artifact 单个参数，弱端点
tool-call 序列化在 ~6KB 截断 —— 审稿全部完成、最后落盘一步崩，run 白跑，
review 门卡死，进而诱发 #202 的 orchestrator 自产 critique 事故。
结构性解法：单次调用只带几百字节，finalize 由框架拼装。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import nodes._reviewer.tools  # noqa: F401  注册工具
from core.state import State
from core.tool_registry import execute as execute_tool


def _state() -> State:
    state = State.new(node_type="_reviewer", base_dir=Path(tempfile.mkdtemp()),
                      project_id="p1")
    state.save_artifact("survey_report", "x", "review subject")
    return state


def _call(state, **kw):
    return asyncio.run(execute_tool("compose_review_critique", state, **kw))


def test_full_flow_produces_valid_artifact():
    """完整流程：verdict + 2 concerns + strengths + action → finalize。"""
    st = _state()
    assert _call(st, action="set_verdict", verdict="approve_with_revisions",
                 confidence=0.88, summary="总体扎实，两处需修")["status"] == "success"
    assert _call(st, action="add_concern", severity="major",
                 title="论文计数不符",
                 description="正文说 18 篇，source_chunks 有 19 条",
                 suggestion="核对后统一")["status"] == "success"
    assert _call(st, action="add_concern", severity="minor",
                 title="Dragonfly 覆盖不足",
                 description="该方向只引了 1 篇")["status"] == "success"
    assert _call(st, action="add_strength",
                 text="检索面广，chunk 溯源完整")["status"] == "success"
    assert _call(st, action="set_recommended_action",
                 recommended_action="revise",
                 feedback_to_next_run="修计数与 Dragonfly 覆盖")["status"] == "success"

    r = _call(st, action="finalize", name="literature_critique_abc123",
              artifact_under_review="survey_report__x",
              source_node_type="literature")
    assert r["status"] == "success"

    rec = st.read_artifact(r["artifact_id"])
    # content 是框架拼的合法 JSON（#202 现象 2 由构造保证不发生）
    obj = json.loads(rec["content"])
    assert obj["verdict"] == "approve_with_revisions"
    assert len(obj["concerns"]) == 2
    assert obj["recommended_action"]["action"] == "revise"
    assert obj["_composed_by"] == "compose_review_critique"
    # metadata 契约字段自动写、与 content 恒一致（decision package 直读）
    md = rec["metadata"]
    assert md["verdict"] == "approve_with_revisions"
    assert md["n_concerns"] == 2
    assert md["n_critical_concerns"] == 0
    assert md["recommended_action"] == "revise"
    # provenance 章（#202）
    assert rec["produced_by_node_type"] == "_reviewer"


def test_project_synthesis_finalize_stamps_writing_gate_contract():
    """The typed reviewer path must mint every field consumed by writing-gate."""
    st = _state()
    assert _call(
        st,
        action="set_verdict",
        verdict="approve",
        confidence=0.94,
        summary="All six readiness dimensions pass.",
    )["status"] == "success"
    assert _call(
        st,
        action="set_project_synthesis",
        project_verdict="ready_to_write",
        mode="research",
        user_requirement_summary="User requested a traceable comparison paper.",
        current_state_summary="Evidence and TCO calculations are complete.",
    )["status"] == "success"
    scores = {
        "narrative_coherence": 5,
        "evidence_sufficiency": 4,
        "direction_validity": 5,
        "coverage": 4,
        "methodological_return": 4,
        "publishability": 4,
    }
    assert _call(st, action="set_scores", scores=scores)["status"] == "success"
    assert _call(
        st,
        action="set_recommended_action",
        recommended_action="proceed",
    )["status"] == "success"

    result = _call(
        st,
        action="finalize",
        name="project_synthesis_ready",
        artifact_under_review="survey_report__x",
        source_node_type="_project",
    )

    assert result["status"] == "success"
    record = st.read_artifact(result["artifact_id"])
    metadata = record["metadata"]
    assert metadata["scope"] == "project_synthesis"
    assert metadata["project_verdict"] == "ready_to_write"
    assert metadata["mode"] == "research"
    assert metadata["scores"] == scores
    assert metadata["actionable_next_steps"] == []
    content = json.loads(record["content"])
    assert content["project_verdict"] == metadata["project_verdict"]
    assert content["user_requirement_summary"] == metadata["user_requirement_summary"]
    assert content["current_state_summary"] == metadata["current_state_summary"]


def test_project_synthesis_ready_rejects_blocking_next_step():
    """A contradictory ready credential must fail before it can open writing-gate."""
    st = _state()
    _call(st, action="set_verdict", verdict="approve", confidence=0.9)
    _call(
        st,
        action="set_project_synthesis",
        project_verdict="ready_to_write",
        mode="research",
        user_requirement_summary="Write the paper.",
        current_state_summary="One evidence gap remains.",
    )
    _call(st, action="set_scores", scores={"publishability": 4})
    _call(
        st,
        action="add_actionable_next_step",
        step_action="fix_evidence_gap",
        step_target_node="literature",
        step_why="One official parameter is missing.",
        step_how="Retrieve and register the official parameter.",
        blocks_writing=True,
    )
    _call(st, action="set_recommended_action", recommended_action="proceed")

    result = _call(
        st,
        action="finalize",
        name="contradictory_project_synthesis",
        artifact_under_review="survey_report__x",
        source_node_type="_project",
    )

    # 判决拆除批 3w（critique_builder.py:389 降格→R-OB1）：意见矛盾
    # （ready_to_write 同时带 blocks_writing step）如实入账随 payload 返回，
    # referee 终审——不再拒绝落盘（389/395 是开火榜第一 writing-gate 的
    # 上游供给端；395 强迫凭空造阻塞项已删）。
    assert result["status"] == "success"
    assert any("blocks_writing=true" in a for a in result["advisories"])
    record = st.read_artifact(result["artifact_id"])
    payload = json.loads(record["content"])
    assert any("blocks_writing=true" in a for a in payload["advisories"])
    assert record["metadata"]["project_verdict"] == "ready_to_write"


def test_each_call_stays_small_even_for_huge_critique():
    """核心验收（#184 E / #202 建议5）：即便 critique 总量远超实测截断点
    （7.7KB），每次调用的参数也只有几百字节 —— 结构上避开长参数序列化。"""
    st = _state()
    _call(st, action="set_verdict", verdict="major_concerns", confidence=0.7)
    per_call_sizes = []
    for i in range(20):                     # 20 条详细 concern，总量 >8KB
        kw = dict(action="add_concern", severity="major",
                  title=f"concern {i}",
                  description="细节说明：" + "x" * 300,
                  suggestion="建议：" + "y" * 100)
        per_call_sizes.append(len(json.dumps(kw, ensure_ascii=False)))
        assert _call(st, **kw)["status"] == "success"
    _call(st, action="set_recommended_action", recommended_action="revise")
    r = _call(
        st,
        action="finalize",
        name="big_critique",
        artifact_under_review="survey_report__x",
    )

    assert r["status"] == "success"
    content = st.read_artifact(r["artifact_id"])["content"]
    assert len(content) > 7710              # 总量超过实测截断的 7.7KB 那单
    assert max(per_call_sizes) < 1500       # 但单次调用从未接近 6KB 截断区
    assert json.loads(content)["verdict"] == "major_concerns"   # 且完好


def test_finalize_records_incomplete_draft_honestly():
    """判决拆除批 3w（critique_builder.py:357 降格→R-OB1）：缺 verdict /
    recommended_action 照落盘——缺项进 advisories，metadata 盖
    review_incomplete=True（账真：审了一半的评审不冒充完整评审，冻结门
    读这个章）。artifact_under_review 仍必填（身份，C）。"""
    st = _state()
    missing_subject = _call(st, action="finalize", name="x")
    assert missing_subject["status"] == "error"
    assert "artifact_under_review" in missing_subject["error"]

    st.save_artifact("survey_report", "x", "正文")
    r = _call(st, action="finalize", name="x",
              artifact_under_review="survey_report__x")
    assert r["status"] == "success"
    assert any("verdict" in a for a in r["advisories"])
    assert any("recommended_action" in a for a in r["advisories"])
    record = st.read_artifact(r["artifact_id"])
    assert record["metadata"]["review_incomplete"] is True
    assert any("verdict" in a for a in record["metadata"]["advisories"])


def test_redirect_requires_target_node():
    """#153 的教训在源头挡：缺 target 的 redirect 下游会被降级，不如这里就拦。"""
    st = _state()
    r = _call(st, action="set_recommended_action",
              recommended_action="redirect_upstream")
    assert r["status"] == "error"
    assert "target_node" in r["error"]
    # data 曾经是合法目标，后来改成了服务节点（post_run_flow: none）—— 退回它，
    # 那条审查义务永远关不掉（见 test_a_redirect_target_must_be_able_to_close_the_flow）。
    bad = _call(st, action="set_recommended_action",
                recommended_action="redirect_upstream", target_node="data")
    assert bad["status"] == "error"
    ok = _call(st, action="set_recommended_action",
               recommended_action="redirect_upstream", target_node="hypothesis")
    assert ok["status"] == "success"


def test_invalid_enums_rejected_at_dispatch_by_schema():
    """判决拆除三波：enum / 区间契约归 schema，派发口核一次并列出合法值。

    工具体内的手写 enum 检查已删；墙没有消失，搬去了 `execute()` 的 schema
    校验 —— 判据是报错带 `parameter_violations` 与 `parameters_schema`。
    """
    st = _state()
    for kw in (
        dict(action="set_verdict", verdict="lgtm"),
        dict(action="set_verdict", verdict="approve", confidence=1.5),
        dict(action="add_concern", severity="blocker", description="x"),
        dict(action="set_project_synthesis", project_verdict="ship_it", mode="research"),
        dict(action="set_project_synthesis", project_verdict="iterate", mode="vibes"),
        dict(action="add_actionable_next_step", step_action="dance", blocks_writing=False),
        dict(action="set_recommended_action", recommended_action="yolo"),
        dict(action="nonsense"),
    ):
        res = _call(st, **kw)
        assert res["status"] == "error", kw
        assert res.get("parameter_violations"), (kw, res)
        assert res.get("parameters_schema"), kw


def test_non_numeric_confidence_is_reported_as_a_shape_mismatch():
    st = _state()
    res = _call(st, action="set_verdict", verdict="approve", confidence="high")
    assert res["status"] == "error"
    assert res.get("argument_shape_mismatches"), res


def test_empty_scores_and_untyped_blocks_writing_are_recorded_not_rejected():
    """从前 set_scores({}) 与缺 blocks_writing 各是一堵墙；现在照收，缺项在
    finalize 如实记进 advisories（把墙加回去这条必转红）。"""
    st = _state()
    assert _call(st, action="set_verdict", verdict="approve", confidence=0.9)["status"] == "success"
    assert _call(st, action="set_scores", scores={})["status"] == "success"
    assert _call(
        st, action="set_project_synthesis", project_verdict="iterate", mode="research",
        user_requirement_summary="u", current_state_summary="c",
    )["status"] == "success"
    step = _call(st, action="add_actionable_next_step", step_action="fix_evidence_gap",
                 step_why="w", step_how="h")
    assert step["status"] == "success", step
    assert _call(st, action="set_recommended_action",
                 recommended_action="revise")["status"] == "success"
    r = _call(st, action="finalize", name="proj_critique_x",
              artifact_under_review="survey_report__x", source_node_type="_project")
    assert r["status"] == "success", r
    advisories = r["advisories"]
    assert any("缺 scores" in a for a in advisories), advisories
    assert any("blocks_writing 未按 boolean 声明" in a for a in advisories), advisories
    rec = st.read_artifact(r["artifact_id"])
    assert rec["metadata"]["advisories"] == advisories
    assert rec["metadata"]["actionable_next_steps"][0]["blocks_writing"] is None


def test_set_verdict_without_verdict_does_not_wipe_a_set_verdict():
    st = _state()
    _call(st, action="set_verdict", verdict="approve")
    assert _call(st, action="set_verdict", confidence=0.5)["status"] == "success"
    assert _call(st, action="status")["draft"]["verdict"] == "approve"


def test_critical_concern_counted_for_red_line():
    """critical concern 计数进 metadata —— decision package 红线判定读它。"""
    st = _state()
    _call(st, action="set_verdict", verdict="block", confidence=0.9)
    _call(st, action="add_concern", severity="critical",
          title="伪造引用", description="claim_9f8f2f80 KB 不存在")
    _call(st, action="set_recommended_action", recommended_action="revise",
          feedback_to_next_run="去掉 phantom citation")
    r = _call(
        st,
        action="finalize",
        name="c",
        artifact_under_review="survey_report__x",
    )
    md = st.read_artifact(r["artifact_id"])["metadata"]
    assert md["n_critical_concerns"] == 1
    # 红线判定读 content concerns 的 description —— 双写保证可读
    obj = json.loads(st.read_artifact(r["artifact_id"])["content"])
    assert obj["concerns"][0]["description"]
    assert obj["concerns"][0]["summary"]


def test_finalize_output_passes_decision_package():
    """端到端：拼出来的 critique 能被 decision package 正常消费（含红线 veto）。"""
    from shared.tools.library.decision_package import (
        _apply_critical_veto,
        _parse_critique_json,
    )
    st = _state()
    _call(st, action="set_verdict", verdict="approve_with_revisions", confidence=0.8)
    _call(st, action="add_concern", severity="critical",
          title="数据造假", description="Figure 3 为 np.random 合成")
    _call(st, action="set_recommended_action", recommended_action="proceed")
    r = _call(
        st,
        action="finalize",
        name="c2",
        artifact_under_review="survey_report__x",
    )

    obj = _parse_critique_json(st.read_artifact(r["artifact_id"])["content"])
    assert obj is not None
    # critical concern 存在时，即便 reviewer 写了 proceed，veto 也必须改写
    action, note = _apply_critical_veto(obj, "proceed", None)
    assert action != "proceed"


def test_draft_persists_across_calls_and_resets_after_finalize():
    st = _state()
    _call(st, action="set_verdict", verdict="approve", confidence=0.95)
    _call(st, action="set_recommended_action", recommended_action="proceed")
    assert _call(st, action="status")["draft"]["verdict"] == "approve"
    _call(
        st,
        action="finalize",
        name="ok",
        artifact_under_review="survey_report__x",
    )
    # finalize 后草稿清空
    assert _call(st, action="status")["draft"]["verdict"] is None


def test_status_reports_missing_pieces():
    st = _state()
    _call(st, action="set_verdict", verdict="approve")
    d = _call(st, action="status")["draft"]
    assert any("recommended_action" in m for m in d["missing_before_finalize"])
