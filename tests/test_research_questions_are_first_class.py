"""研究问题是一等公民，假设只是"带命题的问题"。

回归锚点是 issue #417/#418 那个真实事故：历史解释类课题为了过阈值门，编出
"差 30 年算削弱、差 20 年算证伪、差 10 个百分点算独特性削弱"。三个数字全是凑的。

这里守两个方向：
  - **放开**：没有命题的研究（探索/表征/方法/解释/复现/推导）能立协议、能进账、
    能被关闭门禁管住 —— 不必编任何数字。
  - **没放开**：闭合条件本身不许省；兑现不了就关不掉；`estimated` 和空口勾除
    都不算兑现。放宽的是形式，不是诚实。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core import prereg_commitments as pc
from core.state import State

# 英国饮食文化那类课题：产出是一个解释，没有命题，一个数字都没有
EXPLANATORY = """# Pre-registration

## Research Questions

### Q1: 英国饮食文化的负面评价何时形成、由什么机制驱动？
- output_kind: 一条有史料支撑的历史解释（竞争解释之间的取舍）
```yaml
- id: RIVALS
  statement: "列出 >=3 条竞争解释，每两条之间至少一件能区分它们的一手史料"
- id: PERIOD
  statement: "1840-1950 关键时段的一手材料逐十年覆盖，缺口显式标注"
```
"""

# 开放探索：承诺的是"怎么找、找到哪算找完"，不是"必须找到什么"
EXPLORATORY = """## Research Questions

### Q1: 该体系低温区有什么相行为？
- output_kind: 一张相图
```yaml
- id: COVERAGE
  statement: "成分 0-1 步长 0.1、温度 100-400K 步长 50K 全部扫完"
- id: QUALITY
  statement: "每个格点能量收敛到 1e-5 eV"
```
"""

# 混合型：一半要判决，一半只是测个数
MIXED = """## Research Questions

### Q1: 冷却速率是否改变势能极小值分布？
- output_kind: 对一条命题的裁决
- proposition: 慢冷样品的 inherent structure 能量显著低于快冷样品
```yaml
- metric: IS_energy_delta
  comparison: "<"
  threshold: -0.05
```

### Q2: 该体系的玻璃化温度是多少？
- output_kind: 一个带不确定度的数
```yaml
- id: AGREE
  statement: "三种独立外推法给出的 95% 区间互相重叠"
```
"""

# 历史格式 —— 已冻结的协议改不了，必须永远读得懂
LEGACY = """## Hypothesis 1 (H1): Routing cuts cost without hurting quality

### Falsification Criteria
```yaml
- metric: cost_reduction_pct
  comparison: greater_than
  threshold: 20.0
```
"""


def _state(tmp_path, prereg, *, measured=None, discharges=None) -> State:
    st = State.new(node_type="experiment", base_dir=tmp_path / "runs",
                   project_id="p_rq")
    st.save_artifact("pre_registration", "Prereg", prereg, {})
    meta: dict = {}
    if measured is not None:
        meta["measured_metrics"] = measured
    if discharges is not None:
        meta["closure_discharges"] = discharges
    if meta:
        st.save_artifact("experiment_log", "Exp", "results", meta)
    return st


def _claim():
    return {"id": "claim_x", "claim_type": "hypothesis", "claim_text": "…"}


# ── 放开：没有命题也是合法研究 ──────────────────────────────────────────


@pytest.mark.parametrize("doc,qid", [(EXPLANATORY, "Q1"), (EXPLORATORY, "Q1")])
def test_a_study_without_any_proposition_is_a_real_study(doc, qid):
    """核心诉求：不写命题、不写任何数字，照样是一份完整的协议。"""
    questions = pc.parse_questions(doc)
    assert list(questions) == [qid]
    q = questions[qid]
    assert q.is_hypothesis is False
    assert q.metrics == []                      # 一个数字都没有
    assert len(q.closure) == 2                  # 但闭合条件齐全
    assert pc.declares_questions(doc) is True
    assert pc.declares_hypotheses(doc) is False
    assert pc.questions_without_closure(doc) == []


def test_mixed_study_declares_per_question_not_per_project():
    """混合型研究：范式路由选哪个都只对一半，按问题声明就没这个问题。"""
    qs = pc.parse_questions(MIXED)
    assert qs["Q1"].is_hypothesis is True
    assert qs["Q2"].is_hypothesis is False
    assert [i.kind for i in qs["Q1"].closure] == [pc.NUMERIC]
    assert [i.kind for i in qs["Q2"].closure] == [pc.STATEMENT]


def test_legacy_protocol_still_parses_as_a_hypothesis_question():
    """已冻结的历史协议改不了 —— 永远读作「带命题的问题，编号 H1」。"""
    qs = pc.parse_questions(LEGACY)
    assert list(qs) == ["H1"]
    assert qs["H1"].is_hypothesis is True
    assert qs["H1"].legacy is True
    assert qs["H1"].metrics == [
        {"metric": "cost_reduction_pct", "comparison": "greater_than",
         "threshold": "20.0"}]
    # v3.8 的兼容视图形状一字不变
    assert pc.parse_commitments(LEGACY)["H1"]["metrics"][0]["metric"] == "cost_reduction_pct"


def test_an_explicitly_empty_proposition_is_not_a_hypothesis():
    """实测抓到的（2026-08-16 英国饮食 A/B 跑）：模型不会把不适用的字段整行删掉，
    它会写 `- **proposition**: （无 —— 这是一个时间定位问题，不是命题裁决）`。

    "字段非空 = 这是假设"会把两个纯解释型问题判成假设，把 falsifier / HIF /
    阈值依据整套拖回来压在它们头上 —— 正是这次要消灭的东西。
    """
    doc = """## Research Questions

### Q1: 负面评价什么时候形成的？
- output_kind: 一个时间线
- **proposition**: （无 —— 这是一个时间定位问题，不是命题裁决）
```yaml
- id: TIMELINE
  statement: "四条机制各给出起止时间窗口，每条至少两类独立史料"
```

### Q2: 英国是不是比同期工业化国家更严重？
- output_kind: 比较历史分析
- proposition: 英国的声誉损失显著高于可比工业化国家
```yaml
- id: CMP
  statement: "至少两个对照国，每个比较维度用同一定义与史料类型"
```
"""
    qs = pc.parse_questions(doc)
    assert qs["Q1"].is_hypothesis is False, "写了「无」的等同于没写"
    assert qs["Q2"].is_hypothesis is True
    assert pc.declares_hypotheses(doc) is True      # 因为 Q2 有命题


@pytest.mark.parametrize("raw", ["无", "none", "N/A", "—", "不适用", "暂无", "（无，纯探索）"])
def test_null_proposition_spellings(raw):
    assert pc.ResearchQuestion(qid="Q1", proposition=raw).is_hypothesis is False


def test_a_real_proposition_is_never_mistaken_for_null():
    """漏判方向必须是安全的：认不出的写法一律当成「有命题」（更严），不 fail-open。"""
    for raw in ["英国声誉损失显著高于对照国", "无关变量不影响收敛速度", "none of the baselines beat ours"]:
        assert pc.ResearchQuestion(qid="Q1", proposition=raw).is_hypothesis is True


# ── 没放开：闭合条件不许省，兑现不了就关不掉 ────────────────────────────


def test_a_question_without_closure_conditions_is_rejected():
    """公理二：开工前必须承诺"怎样算答完"。没有它这个问题永远关不掉。"""
    doc = "## Research Questions\n\n### Q1: 随便问点什么？\n- output_kind: 一个解释\n"
    assert pc.questions_without_closure(doc) == ["Q1"]


def test_statement_items_gate_the_ledger_just_like_metrics(tmp_path):
    """陈述条进账、欠账、上门禁 —— 跟数值条同等待遇。"""
    st = _state(tmp_path, EXPLORATORY)
    unfulfilled = pc.unfulfilled_items(st, "Q1")
    assert {r["key"] for r in unfulfilled} == {"COVERAGE", "QUALITY"}
    assert all(r["kind"] == pc.STATEMENT for r in unfulfilled)

    brief = pc.render_commitment_brief(st)
    assert brief and "COVERAGE" in brief and "Q1" in brief


def test_discharging_every_item_clears_the_ledger(tmp_path):
    st = _state(tmp_path, EXPLORATORY, discharges={
        "COVERAGE": {"status": "discharged", "evidence": "experiment_log__Sweep"},
        "QUALITY": {"status": "discharged", "evidence": "experiment_log__Sweep"},
    })
    assert pc.unfulfilled_items(st, "Q1") == []


def test_discharged_without_evidence_does_not_count(tmp_path):
    """空口勾除等于没勾 —— 与"estimated 不算测量"同一条红线。"""
    st = _state(tmp_path, EXPLORATORY, discharges={
        "COVERAGE": {"status": "discharged"},                       # 缺 evidence
        "QUALITY": {"status": "discharged", "evidence": "log__x"},
    })
    rows = pc.unfulfilled_items(st, "Q1")
    assert [r["key"] for r in rows] == ["COVERAGE"]
    assert "evidence" in rows[0]["declared_status"]


def test_a_failed_item_keeps_the_question_open(tmp_path):
    """扫不完就是失败 —— 失败条件不用单独写，它就是"兑现不了"。"""
    st = _state(tmp_path, EXPLORATORY, discharges={
        "COVERAGE": {"status": "failed", "note": "高温段机时不够"},
        "QUALITY": {"status": "discharged", "evidence": "log__x"},
    })
    assert [r["key"] for r in pc.unfulfilled_items(st, "Q1")] == ["COVERAGE"]


def test_mixed_question_cannot_be_closed_until_both_kinds_are_done(tmp_path):
    """闭合条件是合取，数值条和陈述条一视同仁。"""
    st = _state(tmp_path, MIXED,
                measured={"IS_energy_delta": {"status": "measured", "value": -0.07}})
    assert pc.unfulfilled_items(st, "Q1") == []          # 数值条兑现了
    assert [r["key"] for r in pc.unfulfilled_items(st, "Q2")] == ["AGREE"]

    # 带命题的 Q1 可以关；Q2 没有命题，本来也不走 claim 翻转这条路
    assert pc.status_flip_block(st, _claim(), "Q1", "refuted") is None


def test_flip_gate_now_covers_question_ids(tmp_path):
    """关闭门禁跟着泛化到问题编号 —— 不再只认 H。"""
    st = _state(tmp_path, MIXED)                          # 什么都没测
    msg = pc.status_flip_block(st, _claim(), "Q1", "validated")
    assert msg and "IS_energy_delta" in msg
    unknown = pc.status_flip_block(st, _claim(), "Q9", "validated")
    assert unknown and "不在冻结预注册里" in unknown


def test_obligations_report_statement_debt_too(tmp_path):
    """欠账机制原来只数没测的量，没有命题的研究因此一分钱都不欠。"""
    from core.obligations import _collect_unmeasured

    st = _state(tmp_path, EXPLORATORY)
    owed = _collect_unmeasured(st, [])
    assert len(owed) == 1
    assert owed[0].extra["question_id"] == "Q1"
    assert owed[0].extra["is_hypothesis"] is False
    assert set(owed[0].extra["statements"]) == {"COVERAGE", "QUALITY"}


# ── 冻结闸 ──────────────────────────────────────────────────────────────


def _freeze(tmp_path, content):
    import asyncio

    from shared.tools.library.artifacts_extra import _freeze_artifact

    st = State.new(node_type="hypothesis", base_dir=tmp_path / "runs",
                   project_id="p_freeze")
    saved = st.save_artifact("pre_registration", "P", content, {})
    return asyncio.run(_freeze_artifact(
        state=st, artifact_id=saved["id"], reason="test",
        run_role="primary", analysis_eligible=True, expected_params={"n": 1},
    ))


def test_freeze_accepts_a_protocol_with_no_numbers_at_all(tmp_path):
    """英国饮食那份协议现在冻得进去 —— 这就是这次改动要的结果。"""
    assert _freeze(tmp_path, EXPLANATORY).get("status") == "success"


def test_freeze_records_a_protocol_with_no_research_questions(tmp_path):
    """公理一：至少一个研究问题。探测是真的，但它预测的是未来的协议偏离——
    判决拆除·第三波：冻结照做，缺口如实写进冻结件 metadata.freeze_warnings
    （终审与 experiment 都看得见）。墙若加回来（拒冻）这条转红。"""
    out = _freeze(tmp_path, "# Pre-registration\n\n随便写点什么。\n")
    assert out.get("status") == "success", out
    assert any("没有任何研究问题" in w for w in out["freeze_warnings"])
    assert "没有任何研究问题" in out["note"]


def test_freeze_records_a_question_that_never_says_when_it_is_done(tmp_path):
    out = _freeze(tmp_path, "## Research Questions\n\n### Q1: 问点什么？\n"
                            "- output_kind: 一个解释\n")
    assert out.get("status") == "success", out
    assert any("没有任何闭合条件" in w for w in out["freeze_warnings"])


# ── v0.5.1：两层楼 + 拔掉数量锚（2026-08-16 英国饮食 A/B 实测后补） ──────


def _goal_brief(tmp_path, inputs: dict) -> tuple[dict, str]:
    """走真实入口 —— briefing 就是节点第 0 轮看到的那段文本。"""
    import asyncio

    from nodes.hypothesis.tools.research_goal import _get_research_goal

    st = State.new(node_type="hypothesis", base_dir=tmp_path / "runs", project_id="p_g")
    st.hook_state["node_inputs"] = inputs
    out = asyncio.run(_get_research_goal(st))
    return st.hook_state["research_goal_parsed"], out["message"]


def test_framework_never_names_a_question_count(tmp_path):
    """实测：默认 target_prereg=3 被渲染进第 0 轮 briefing，模型就正好交 3 个。

    问题数只有一个合法来源 —— 用户诉求拆出来是几个就是几个。框架在任何位置
    报出一个具体数字，那个数字就会变成锚。
    """
    parsed, brief = _goal_brief(tmp_path, {"research_question": "英国饮食文化为什么评价不高？"})
    assert parsed["iteration"]["target_prereg"] is None
    assert parsed["iteration"]["min_candidates"] is None
    assert "由课题决定" in brief
    assert "target=" not in brief, "briefing 不许报目标数"
    assert "min_candidates=" not in brief


def test_caller_specified_budget_is_passed_through(tmp_path):
    """调用方**自己**要几个，框架原样转达 —— 禁止的是框架发明数字。"""
    parsed, brief = _goal_brief(
        tmp_path, {"research_question": "x", "hypothesis_iteration": {"target_prereg": 2}})
    assert parsed["iteration"]["target_prereg"] == 2
    assert "2" in brief


_EXPLORATORY_WITH_ASSUMPTION = """## Research Questions

### Q1: 该体系低温区有什么相行为？
- output_kind: 一张相图
- assumption: 值得看的相变落在 100-400K 这个窗口内
```yaml
- id: COVERAGE
  statement: "成分 0-1 步长 0.1、温度 100-400K 步长 50K 全部扫完"
```
"""


def test_a_concise_output_kind_is_not_too_short():
    """「一张相图」四个字被 `>=6 字` 判过短 —— 查长度就是查形式，会逼出凑字。"""
    from nodes.hypothesis.tools.research_questions import assess_research_questions

    assert assess_research_questions(_EXPLORATORY_WITH_ASSUMPTION)["passed"] is True


def test_vague_output_kind_still_rejected():
    """放宽的是长度，不是空话。"""
    from nodes.hypothesis.tools.research_questions import assess_research_questions

    doc = _EXPLORATORY_WITH_ASSUMPTION.replace("- output_kind: 一张相图",
                                               "- output_kind: 待定")
    assert assess_research_questions(doc)["passed"] is False


def test_every_question_must_declare_its_assumptions():
    """探索型也要 —— 「扫这个区间」预设了答案在区间里。"""
    from nodes.hypothesis.tools.research_questions import assess_research_questions

    doc = _EXPLORATORY_WITH_ASSUMPTION.replace(
        "- assumption: 值得看的相变落在 100-400K 这个窗口内\n", "")
    report = assess_research_questions(doc)
    assert report["passed"] is False
    assert any("assumption" in p for p in report["problems"])
    assert pc.parse_questions(_EXPLORATORY_WITH_ASSUMPTION)["Q1"].assumptions


def test_restatement_check_covers_questions_without_a_proposition(tmp_path):
    """核心回归：无命题的问题也要查「是不是把已知结论换个说法」。

    上一版这条挂在 claim 上，而无命题的问题不产生 claim —— 于是探索/表征/解释
    型研究一项内容质量判据都拿不到（实测 3 个问题只审了 1 个）。
    """
    from nodes.hypothesis.tools.research_questions import assess_question_restatement

    finding = "NHC chain 在高密度 LJ 上比 single Nosé-Hoover 收敛更快且更 ergodic"
    st = State.new(node_type="hypothesis", base_dir=tmp_path / "runs", project_id="p_rs")
    st.save_artifact("survey_report", "S", f"## Key findings\n\n1. {finding}\n")
    st.save_artifact("pre_registration", "P", f"""## Research Questions

### Q1: {finding}？
- output_kind: 一个结论
- assumption: 高密度区确有可测的收敛差异
```yaml
- id: C
  statement: "两种 thermostat 各跑 10 次取收敛时间中位数"
```
""", {})
    report = assess_question_restatement(st)
    assert report["applicable"] is True
    assert report["passed"] is False, "问题几乎照抄 survey finding，必须被标出"
    assert report["flagged"][0]["label"] == "Q1"


def test_value_assessment_must_cover_every_question(tmp_path):
    """价值/可信性评估按**问题**对账，缺谁报谁 —— 不再只评有 claim 的那一条。"""
    import json as _json

    from nodes.hypothesis.tools.research_questions import assess_question_value_coverage

    st = State.new(node_type="hypothesis", base_dir=tmp_path / "runs", project_id="p_v")
    st.save_artifact("pre_registration", "P", MIXED.replace(
        "- output_kind: 对一条命题的裁决",
        "- output_kind: 对一条命题的裁决\n- assumption: 冷却速率区间覆盖了会变化的那段", 1))
    st.save_artifact("hypothesis_innovation_report", "HIF",
                     "```json\n" + _json.dumps({"assessments": [{"label": "Q1"}]}) + "\n```")
    report = assess_question_value_coverage(st)
    assert report["passed"] is False
    assert report["missing"] == ["Q2"], "Q2 没评价值，必须报出来"


def test_value_coverage_accepts_a_markdown_assessment(tmp_path):
    """实测（2026-08-16 单问题课题）：模型把价值评估写成 markdown 表格 ——
    `### Q1: …` + G/D/M/P 标 N/A + R/Q/I 打分，完全合格。

    只认 JSON 里 `"label": "Q1"` 的对账判它「没评」。**认序列化格式不认事实**，
    跟长度阈值是同一个病。
    """
    from nodes.hypothesis.tools.research_questions import assess_question_value_coverage

    st = State.new(node_type="hypothesis", base_dir=tmp_path / "runs", project_id="p_md")
    st.save_artifact("pre_registration", "P", EXPLORATORY.replace(
        "- output_kind: 一张相图",
        "- output_kind: 一张相图\n- assumption: 相变落在该窗口内", 1))
    st.save_artifact("hypothesis_innovation_report", "HIF", """# HIF Report

## 研究问题

### Q1: 该体系低温区有什么相行为？
- 无 proposition → 不适用 G/D/M/P 维度

| 维度 | 评分 | 说明 |
|---|---|---|
| G | N/A | 非命题裁决型 |
| R | 2 | KB 中无相同记录 |
| Q | 4 | 问题完全合理 |
| I | 3 | 有直接实用价值 |
""")
    assert assess_question_value_coverage(st)["passed"] is True


# ── 2026-08-17 平台真跑（LJ 课题）抓到的两个缺陷 ──────────────────────────


def test_threshold_rationale_survives_parsing():
    """协议里写全了阈值依据，解析时**不许丢**。

    实测：节点把 literature/DOI/科学含义都写进了 `threshold_rationale`，
    解析器只带出 metric/comparison/threshold，阈值审计因此永远判「缺依据」。
    协议已冻结改不动 —— 节点连试 12 次，最后只能 report_blocker 收场。
    **解析器丢掉的字段，下游没有任何办法补回来。**
    """
    doc = """## Research Questions

### Q1: D(T) 是否服从 Arrhenius？
- output_kind: 对一条命题的裁决
- proposition: 在 rho*=0.85、T*=0.8–2.0 区间 D(T) 服从 Arrhenius 形式
- assumption: 该温区内不发生相变
```yaml
- metric: arrhenius_fit_r2
  comparison: ">="
  threshold: 0.99
  threshold_rationale:
    source_type: literature
    citation_or_derivation: "Meier et al. 2004 高精度基准，DOI 10.1063/1.1770695"
    scientific_meaning: "R2 低于该值说明单一激活能模型不足以描述该温区"
- id: RESID
  statement: "拟合残差随温度无系统性趋势"
```
"""
    item = next(i for i in pc.parse_questions(doc)["Q1"].closure if i.kind == pc.NUMERIC)
    assert item.rationale.get("source_type") == "literature"
    assert "10.1063" in item.rationale.get("citation_or_derivation", "")
    assert item.rationale.get("scientific_meaning")

    from nodes.hypothesis.committed import _extract_yaml_block_falsifiers
    from nodes.hypothesis.tools.threshold_grounding import (
        assess_threshold_grounding,
    )
    fs = _extract_yaml_block_falsifiers(doc)
    assert fs and fs[0]["threshold_rationale"]["source_type"] == "literature"
    assert assess_threshold_grounding(fs)["passed"] is True, "依据齐全就该过"


def test_a_question_is_not_a_restatement_of_its_own_fresh_claim(tmp_path):
    """本轮刚登记的 hypothesis claim 不是「已知结论」—— 别拿问题跟自己比。

    实测：节点交付齐全却 blocked，自己在报告里写「`research_questions_not_
    restatement` 将本 run 刚创建的 hypothesis claim 误判为已知结论」。
    根因：查重把 status="open" 也当已知，而新建 claim 默认就是 open，
    正文又正是从这个问题的 proposition 抄来的 → 100% 自我重合。
    """
    from nodes.hypothesis.tools.research_questions import assess_question_restatement

    prop = "在 rho*=0.85、T*=0.8–2.0 区间 D(T) 服从 Arrhenius 形式"
    doc = f"""## Research Questions

### Q1: 该区间 D(T) 是否服从 Arrhenius？
- output_kind: 对一条命题的裁决
- proposition: {prop}
- assumption: 该温区不发生相变
```yaml
- metric: r2
  comparison: ">="
  threshold: 0.99
```
"""
    st = State.new(node_type="hypothesis", base_dir=tmp_path / "runs", project_id="p_selfcmp")
    _frozen_saved = st.save_artifact("pre_registration", "P", doc, {})
    st.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    chunk, _ = st.write_kb("chunks", {
        "text": doc, "source": "artifact:pre_registration__P",
        "origin_artifact_id": "pre_registration__P",
        "origin_artifact_frozen": True, "offset": 0, "length": len(doc)})
    st.write_kb("claims", {
        "claim_text": prop, "claim_type": "hypothesis",
        "prereg_chunk_id": chunk["id"], "sources": [chunk["id"]],
        "orphan_reason": "测试桩", "falsification_criteria_text": "R2<0.99 则证伪",
        "predicted_outcome": "服从"})

    assert assess_question_restatement(st)["passed"] is True

    # 但**真正已证实**的结论仍然要拦住 —— 放宽的是自我比对，不是查重本身
    st.save_artifact("survey_report", "S", f"## Key findings\n\n1. {prop}\n")
    report = assess_question_restatement(st)
    assert report["passed"] is False, "跟 survey 已报告的结论重合，仍必须标出"
