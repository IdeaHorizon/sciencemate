"""validate_hypothesis_outputs — hypothesis 节点内部成果自检。

结果写入 `hypothesis_output_validation` artifact；framework
`completion_criteria.quality_checks` 机械读取 metadata.passed，
passed=false 或未跑验证 → 节点 status=incomplete（防假完成）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State

from ..committed import committed_claim_texts, committed_falsifiers
from core.tool_registry import ToolDefinition, register_tool

from .artifact_save import save_hypothesis_singleton
from .workflow_graph import validate_mermaid_render_safe, validate_workflow_dag
from .workflow_science import (
    format_science_report,
    parse_task_rows_for_science,
    validate_workflow_science,
)
from .goal_alignment import assess_goal_coverage, resolve_must_cover_themes
from .definition_lock import assess_definition_fidelity, resolve_locked_definitions
from .threshold_grounding import (
    assess_threshold_grounding,
    collect_falsifiers_for_audit,
)
from .comparison_protocol import (
    assess_comparison_protocol,
    collect_comparison_audit_inputs,
)
from .resource_feasibility import (
    assess_resource_feasibility,
    collect_resource_feasibility_inputs,
)
from .cost_instrumentation import (
    assess_cost_instrumentation,
    collect_cost_instrumentation_inputs,
)
from .analysis_mode import (
    MODE_PLAN,
    MODE_REVISE,
    current_mode,
    frozen_prereg_entries,
    run_authored_commitments,
    unaccounted_experiments,
    unresolvable_adjudication_evidence,
)
from .research_questions import (
    assess_question_plan_coverage,
    assess_question_restatement,
    assess_question_value_coverage,
    assess_research_questions,
    latest_prereg_content,
    state_expects_hypotheses,
)

_RESEARCH_PLAN_KEYWORDS = (
    "experimental_design",
    "computational_workflow",
    "baselines",
    "resource_estimates",
    "risk_analysis",
)
_RESEARCH_PLAN_HEADER_SYNS: dict[str, tuple[str, ...]] = {
    "experimental_design": ("experimental design", "experimental_design"),
    "computational_workflow": (
        "computational workflow",
        "computational_workflow",
        "计算工作流",
        "计算流程",
    ),
    "baselines": ("baseline", "baselines"),
    "resource_estimates": ("resource estimate", "resource_estimates"),
    "risk_analysis": ("risk analysis", "risk_analysis"),
}

# ── 两层判定（v2.1 P-QC2，2026-08-08）───────────────────────────────────
# E2E v9 实测：research_plan 的 workflow DAG 里一条循环边 → validator passed=false
# → 节点 incomplete → 框架拒绝 review → orchestrator 只能绕道即兴。一条结构瑕疵
# 挡住整个研究 —— 它本该是 reviewer 的修订意见，不该是完成门。
#
# 拆法：**科学凭据**（防任务退化/改写定义/拍脑袋阈值/不可比硬比/无确认承诺 ——
# 每条都是历轮 E2E 用真事故换来的红线）继续一票否决；**结构质量**（计划章节
# 齐不齐、DAG 顺不顺、overview 在不在）降为 advisory：如实记录、随验证报告
# 交给 reviewer 当修订项，但不把 run 判死。
# metadata.passed 从此 = "科学凭据全过"；advisory_failures 单列。
_ADVISORY_CHECKS = frozenset({
    "hypothesis_innovation_assessed",
    "research_plan_complete",
    "computational_workflow_coherence",
    "research_overview_present",
    # ── 判决拆除批 3w（docs/verdict_demolition/verdicts_writing_nodes.md，
    # hypothesis 节 → H-OB1 研究问题纪律，2026-08-31）────────────────────────
    # 检查照跑、结果如实进报告交 reviewer；拒绝分支死（S2/S3）：
    #   research_questions_declared        ← research_questions.py:131（非空闸）
    #   research_questions_covered_by_plan ← research_questions.py:230/242
    #   research_questions_not_restatement ← research_questions.py:315
    #                                        （0.72 阈值只出信号，不判死）
    #   research_questions_value_assessed  ← research_questions.py:335/356
    #   threshold_grounding                ← threshold_grounding.py:324
    #                                        （解析失败与真无 falsifier 分开报）
    "research_questions_declared",
    "research_questions_covered_by_plan",
    "research_questions_not_restatement",
    "research_questions_value_assessed",
    "threshold_grounding",
})

_CHECK_NAMES = (
    "research_questions_declared",
    "research_questions_covered_by_plan",
    "research_questions_not_restatement",
    "research_questions_value_assessed",
    "prereg_frozen_and_registered",
    "structured_falsification_present",
    "hypothesis_innovation_assessed",
    "hif_plausibility_gate",
    "research_plan_complete",
    "computational_workflow_coherence",
    "not_conclusion_restatement",
    "research_overview_present",
    "user_goal_coverage",
    "definition_fidelity",
    "threshold_grounding",
    "comparison_protocol_complete",
    "resource_feasibility",
    "cost_instrumentation",
    "experiment_results_accounted",
    "adjudication_evidence_resolvable",
)

# ── 判据的适用范围（2026-08-16）────────────────────────────────────────
# 两条正交的适用轴，都**机械推导**，不靠模型或调用方申报：
#
# A. `authored`  —— 只在本轮**新增了科学承诺**（新假说 / 新 prereg / 新冻结）
#    时适用。事故：这几条判据从 per-run 的 transcript 取料，而 artifacts 是
#    跨 run 持久的。第 2 轮开局 transcript 是空的 → 判据全 fail → 节点被迫把
#    "生成→打分→冻结"整套重跑一遍，只为让 transcript 里有那几条调用。
#    prompt 写着"后续轮不要从零重做"，机制却说"不重做就判死"。
#
# B. `hypothesis` —— 只在本项目**有带命题的研究问题**时适用（见
#    research_questions）。产出是一个数/一张图/一个方法/一个解释/一次复现的
#    研究没有假设，也就没有 falsifier、没有 HIF、没有阈值依据可审。硬要它有，
#    就是英国饮食文化 E2E 里那三个凭空编出来的年份阈值。
_AUTHORED_ONLY_CHECKS = frozenset({
    "structured_falsification_present",
    "hif_plausibility_gate",
    "not_conclusion_restatement",
    "hypothesis_innovation_assessed",
    # threshold_grounding 守的是"**别冻**一个没依据的数字"。协议一旦冻结，
    # 阈值依据就改不动了 —— 后续轮再审它只能产出一个无法修复的 fail，节点在
    # 这道门上没有出口（issue #409 的同一个病灶）。它属于"本轮新写的承诺"。
    "threshold_grounding",
})
# ── 两层楼（v0.5.1，2026-08-16 A/B 实测后修正）──────────────────────────
# 第一层：所有研究问题都要过（含带命题的）。见 research_questions.py。
# 第二层：**只有写了 proposition 的**额外加码 —— 就是下面这张表。
#
# 上一版把「非结论复述 / 价值可信性 / scope」也放进了第二层，后果是无命题的
# 问题一项内容质量判据都拿不到（英国饮食实测：3 个问题只审了 1 个，而 Q1
# 「负面评价在什么时期形成并固化？」偷偷把待检验的前提当成了背景，没人管）。
#
# 留在第二层的判据，每条都有它非命题不可的理由：
#   structured_falsification_present / threshold_grounding
#       —— 没有命题就没有要证伪的东西，更没有阈值
#   hypothesis_innovation_assessed
#       —— HIF 报告**存在性**由第一层的 value_assessed 按问题对账，
#          这条只管"报告本身格式对不对"，仍是 claim 侧的事
_HYPOTHESIS_ONLY_CHECKS = frozenset({
    "structured_falsification_present",
    "hypothesis_innovation_assessed",
    "not_conclusion_restatement",
    "hif_plausibility_gate",
    "threshold_grounding",
})
# 计划形态类判据（research_plan_complete / comparison_protocol / resource_
# feasibility / cost_instrumentation …）**不做模式豁免**：它们全部从 artifact
# 取料，第 2 轮读到的还是上一轮那份通过了的正文，天然会过。给它们开豁免只会
# 多出一条"revise 轮可以交一份烂计划"的绕行路径。

# 新增的两条兑现判据（experiment_results_accounted /
# adjudication_evidence_resolvable）不按 mode 开关，按**数据在不在**自门控：
# 没有实验结果就没有要交代的、没有裁决行就没有要核对的证据。按 mode 写死会
# 漏掉"plan 轮里顺手裁决了上一批结果"这种真实情形。


def _research_plan_workflow_ok(content: str) -> tuple[bool, str]:
    """Check computational_workflow structure only (domain-agnostic).

    Mechanical gate: mermaid + Step-ID task table with ≥1 auditable row + DAG.
    Does NOT whitelist discipline-specific task names (DFT/phonon/…); whether the
    chosen steps fit the research domain is for the reviewer Agent (review_spec).
    """
    lower = content.lower()
    if "```mermaid" not in lower:
        return False, "computational_workflow 缺少 mermaid 流程图 (```mermaid)"
    if "flowchart" not in lower:
        return False, "mermaid 块内缺少 flowchart 图"
    if not re.search(
        r"\|\s*step\s*id\s*\|",
        content,
        flags=re.IGNORECASE,
    ):
        return False, "computational_workflow 缺少逐步计算任务表 (| Step ID | ... |)"

    stats: dict[str, Any] = {}
    rows = parse_task_rows_for_science(content, stats=stats)
    if not rows:
        ignored = stats.get("non_workflow_tables_ignored", 0)
        hint = (
            f"（检测到 {ignored} 张非 workflow 表被忽略；请补一张含关键参数/产出/falsifier 列的任务表）"
            if ignored
            else "（未解析到含可追溯列的 workflow 任务表）"
        )
        return False, f"computational_workflow 无可审计任务行 {hint}"

    syntax_ok, syntax_issues, _ = validate_mermaid_render_safe(content)
    if not syntax_ok:
        preview = "; ".join(syntax_issues[:3])
        extra = f" (+{len(syntax_issues) - 3} more)" if len(syntax_issues) > 3 else ""
        return False, f"mermaid 语法错误: {preview}{extra}"

    dag_ok, dag_issues, dag_meta = validate_workflow_dag(content)
    if not dag_ok:
        preview = "; ".join(dag_issues[:3])
        extra = f" (+{len(dag_issues) - 3} more)" if len(dag_issues) > 3 else ""
        return False, f"workflow DAG 不一致: {preview}{extra}"

    fork_n = len(dag_meta.get("fork_nodes") or [])
    join_n = len(dag_meta.get("join_nodes") or [])
    gate_n = len(dag_meta.get("gate_nodes") or [])
    n_types = len({r.task_type for r in rows if r.task_type})
    shape = f"rows={len(rows)} types={n_types} fork={fork_n} join={join_n} gate={gate_n}"
    return True, f"computational_workflow 含 mermaid + 任务表 + DAG 一致 ({shape})"





def _parse_json_block(content: str) -> dict[str, Any] | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", content, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _read_latest_artifact(state: State, artifact_type: str) -> dict[str, Any] | None:
    arts = state.list_artifacts(artifact_type)
    if not arts:
        return None
    return state.read_artifact(arts[-1]["id"])


def _project_expects_hypotheses(state: State) -> bool:
    """本项目是否**有假说要审** —— 假说类判据的适用开关。

    ⚠️ 判据是"到处都找不到假说"，不是"prereg 没写 Inquiry Contract"。
    只看 prereg 格式会开一个 fail-open 的洞：prereg 用了别的写法（或还只是
    `#` 一个占位），而 `create_claim(claim_type='hypothesis')` 已经登记了三条
    命题 —— 那时假说类判据会被整组跳过，falsifier / 阈值依据一条都没人审。
    放宽"必须有假设"不能顺带打开"有假设也不审"。

    三条来源任一命中就算有：契约声明的裁决型问题 / prereg 的 `## Hypothesis N (Hx)`
    命题声明（这条在 research_questions 里）/ 实际存在的 hypothesis 承诺。
    """
    try:
        if state_expects_hypotheses(state):
            return True
    except Exception:
        return True      # 判不了就按老行为（要求假设），不静默放行
    try:
        if committed_falsifiers(state):
            return True
    except Exception:
        return True
    return False


def _applicability(state: State) -> dict[str, str]:
    """每条判据这一轮适不适用；不适用的给出**原因**（进报告，不静默跳过）。"""
    authored = run_authored_commitments(state)
    expects_hypotheses = _project_expects_hypotheses(state)
    mode = current_mode(state)
    skipped: dict[str, str] = {}
    for name in _CHECK_NAMES:
        if name in _HYPOTHESIS_ONLY_CHECKS and not expects_hypotheses:
            skipped[name] = (
                "本项目的 Inquiry Contract 未声明命题裁决型问题"
                "（decides_a_proposition 全为 no）→ 没有假说，也就没有 "
                "falsifier / HIF / 阈值依据可审"
            )
        elif name in _AUTHORED_ONLY_CHECKS and not authored:
            skipped[name] = (
                f"本轮（mode={mode}）没有新增科学承诺 —— 沿用已冻结的协议。"
                "这几条审的是「本轮新写下的假说」，重跑它们等于逼节点把整套"
                "生成/打分/冻结再演一遍"
            )
    return skipped


def _run_check(name: str, state: State) -> dict[str, Any]:
    passed, reasoning = False, "unknown check"

    if name == "research_questions_declared":
        report = assess_research_questions(latest_prereg_content(state))
        passed = bool(report["passed"])
        reasoning = str(report["reason"])

    elif name == "research_questions_not_restatement":
        report = assess_question_restatement(state)
        passed = bool(report["passed"])
        reasoning = str(report["reason"])

    elif name == "research_questions_value_assessed":
        report = assess_question_value_coverage(state)
        passed = bool(report["passed"])
        reasoning = str(report["reason"])

    elif name == "research_questions_covered_by_plan":
        plan = (_read_latest_artifact(state, "research_plan") or {}).get("content") or ""
        report = assess_question_plan_coverage(latest_prereg_content(state), str(plan))
        passed = bool(report["passed"])
        reasoning = str(report["reason"])

    elif name == "prereg_frozen_and_registered":
        # 判据对象是**项目现在有什么协议**，不是本轮说过什么话。
        # 旧版三条都从 per-run transcript 取（freeze 调用、claim 写入），于是
        # 第 2 轮开局必然全 fail —— 协议明明冻在盘上。
        frozen = frozen_prereg_entries(state)
        if not frozen:
            reasoning = "没有已冻结的 pre_registration"
        elif not _project_expects_hypotheses(state):
            passed = True
            reasoning = (
                f"pre_registration 已冻结（{frozen[-1]['id']}）；"
                "本项目未声明命题裁决型问题，无 hypothesis claim 需要关联"
            )
        else:
            # 冻结即承诺完成。KB 侧的 hypothesis claim 由 _curator 在 Analysis
            # 采纳后写入（create_claim 时 sources 里的 artifact 自动固化为
            # chunk）。不再要求提案期就有 KB 关联 —— 那正是被关掉的绕道：
            # 承诺进 KB 无修订语义，2026-08-19 五次修不掉的根源。
            passed = True
            reasoning = (
                f"pre_registration 已冻结（{frozen[-1]['id']}）。"
                "命题的 KB 写入发生在 curator 采纳时，不在提案期。"
            )

    elif name == "structured_falsification_present":
        # 承诺 = 预注册 head 里的证伪判据（nodes.hypothesis.committed）。
        # 这里原来数的是本轮 create_claim 写入 —— 那条路已关（承诺不进 KB），
        # 修订走 prereg 的 amend 链，审计和修订看同一份 head。
        falsifiers = committed_falsifiers(state)
        if falsifiers:
            passed = True
            reasoning = f"预注册 head 声明了 {len(falsifiers)} 条结构化证伪判据"
        else:
            content = latest_prereg_content(state)
            if not content:
                reasoning = "尚无预注册 —— 先 save_artifact(pre_registration)"
            elif "proposition" in content or "命题" in content:
                reasoning = (
                    "预注册声明了命题，却解析不出任何结构化证伪判据。"
                    "在 prereg 里给每条命题写 falsification_criteria_structured"
                    "（qualitative 用 criterion 写清可观察条件）；prereg 已冻结时"
                    "用 save_artifact(同 type 同 name, amendment_reason=...) 修订。"
                )
            else:
                passed = True
                reasoning = "无命题型问题（纯陈述条闭合），不要求证伪判据"

    elif name == "hypothesis_innovation_assessed":
        rec = _read_latest_artifact(state, "hypothesis_innovation_report")
        if not rec:
            reasoning = "缺少 hypothesis_innovation_report"
        else:
            md = rec.get("metadata") or {}
            body = _parse_json_block(rec.get("content") or "")
            if ("max_hif" in md and "n_assessed" in md) or (
                body and body.get("summary", {}).get("n_assessed")
            ):
                passed = True
                reasoning = "hypothesis_innovation_report 有效"
            else:
                reasoning = "hypothesis_innovation_report metadata 不完整"

    elif name == "hif_plausibility_gate":
        hif_rec = _read_latest_artifact(state, "hypothesis_innovation_report")
        if not hif_rec:
            reasoning = "缺少 HIF 报告"
        else:
            hif_body = _parse_json_block(hif_rec.get("content") or "")
            if not hif_body:
                reasoning = "HIF 报告无 JSON"
            else:
                final_texts = {
                    t.lower() for t in committed_claim_texts(state) if t
                }
                rejected: list[str] = []
                for a in hif_body.get("assessments") or []:
                    claim = (a.get("claim_text") or "").strip()
                    if claim.lower() not in final_texts:
                        continue
                    if a.get("plausibility_reject"):
                        rejected.append(a.get("label", "?"))
                    else:
                        dims = a.get("dimensions") or {}
                        q = dims.get("Q")
                        if q is not None and int(q) <= 1:
                            rejected.append(f"{a.get('label', '?')}: Q={q}")
                if rejected:
                    reasoning = f"plausibility_reject: {', '.join(rejected)}"
                else:
                    passed = True
                    reasoning = "无 Q≤1 / plausibility_reject 的最终假设"

    elif name == "research_overview_present":
        rec = _read_latest_artifact(state, "hypothesis_research_overview")
        if not rec:
            reasoning = "缺少 hypothesis_research_overview artifact"
        else:
            content = rec.get("content") or ""
            if len(content.strip()) < 200:
                reasoning = "hypothesis_research_overview 内容过短"
            else:
                passed = True
                reasoning = "hypothesis_research_overview 已保存"

    elif name == "research_plan_complete":
        rec = _read_latest_artifact(state, "research_plan")
        if not rec:
            reasoning = "缺少 research_plan"
        else:
            content = rec.get("content") or ""
            lower = content.lower()
            if all(kw in lower[:900] for kw in _RESEARCH_PLAN_KEYWORDS):
                wf_ok, wf_reason = _research_plan_workflow_ok(content)
                if wf_ok:
                    passed = True
                    reasoning = "Section Index 含五件套 + computational_workflow 完整"
                else:
                    reasoning = wf_reason
            else:
                headers = re.findall(r"^#{1,6}\s+.+?$", content, flags=re.MULTILINE)
                header_blob = " ".join(headers).lower()
                missing = [
                    s for s, syns in _RESEARCH_PLAN_HEADER_SYNS.items()
                    if not any(x in header_blob or x in lower for x in syns)
                    and not (s == "baselines" and (
                        "primary baseline" in lower or "why this baseline" in lower
                    ))
                ]
                if missing:
                    reasoning = f"research_plan 缺 section: {', '.join(missing)}"
                else:
                    wf_ok, wf_reason = _research_plan_workflow_ok(content)
                    if wf_ok:
                        passed = True
                        reasoning = "五件套 section 可见 + computational_workflow 完整"
                    else:
                        reasoning = wf_reason

    elif name == "computational_workflow_coherence":
        rec = _read_latest_artifact(state, "research_plan")
        if not rec:
            reasoning = "缺少 research_plan"
        else:
            report = validate_workflow_science(rec.get("content") or "")
            if report.ok:
                passed = True
                if report.warnings:
                    reasoning = format_science_report(report)
                else:
                    reasoning = "computational_workflow 建模选择可追溯、跨步一致"
            else:
                reasoning = format_science_report(report)

    elif name == "not_conclusion_restatement":
        hif_rec = _read_latest_artifact(state, "hypothesis_innovation_report")
        if not hif_rec:
            reasoning = "缺少 HIF 报告"
        else:
            hif_body = _parse_json_block(hif_rec.get("content") or "")
            if not hif_body:
                reasoning = "HIF 报告无 JSON"
            else:
                final_texts = {
                    t.lower() for t in committed_claim_texts(state) if t
                }
                flagged: list[str] = []
                for a in hif_body.get("assessments") or []:
                    claim = (a.get("claim_text") or "").strip()
                    if claim.lower() not in final_texts:
                        continue
                    dims = a.get("dimensions") or {}
                    r_score = int(dims.get("R", a.get("R", 0)) or 0)
                    tier = a.get("tier", "")
                    if r_score >= 4 or tier == "minimal":
                        flagged.append(f"{a.get('label', '?')}: R={r_score}, tier={tier}")
                audit_rec = _read_latest_artifact(state, "hypothesis_conclusion_audit")
                if audit_rec:
                    audit_body = _parse_json_block(audit_rec.get("content") or "")
                    if audit_body:
                        for item in audit_body.get("flagged", []):
                            flagged.append(
                                f"{item.get('label', '?')}: {item.get('reason', 'overlap')}"
                            )
                if flagged:
                    reasoning = "疑似结论复述: " + "; ".join(flagged)
                else:
                    passed = True
                    reasoning = f"已审核 {len(final_texts)} 条，无结论复述标记"

    elif name == "user_goal_coverage":
        themes = resolve_must_cover_themes(state)
        prereg = (_read_latest_artifact(state, "pre_registration") or {}).get("content") or ""
        plan = (_read_latest_artifact(state, "research_plan") or {}).get("content") or ""
        overview = (
            (_read_latest_artifact(state, "hypothesis_research_overview") or {}).get("content")
            or ""
        )
        report = assess_goal_coverage(
            themes, prereg=str(prereg), plan=str(plan), overview=str(overview),
        )
        passed = bool(report["passed"])
        reasoning = str(report["reason"])

    elif name == "definition_fidelity":
        locked = resolve_locked_definitions(state)
        prereg = (_read_latest_artifact(state, "pre_registration") or {}).get("content") or ""
        plan = (_read_latest_artifact(state, "research_plan") or {}).get("content") or ""
        overview = (
            (_read_latest_artifact(state, "hypothesis_research_overview") or {}).get("content")
            or ""
        )
        report = assess_definition_fidelity(
            locked, prereg=str(prereg), plan=str(plan), overview=str(overview),
        )
        passed = bool(report["passed"])
        reasoning = str(report["reason"])

    elif name == "threshold_grounding":
        falsifiers = collect_falsifiers_for_audit(state)
        report = assess_threshold_grounding(falsifiers)
        passed = bool(report["passed"])
        reasoning = str(report["reason"])

    elif name == "comparison_protocol_complete":
        inputs = collect_comparison_audit_inputs(state)
        force_cross = None
        force_hetero = None
        ni = state.hook_state.get("node_inputs") or {}
        if isinstance(ni, dict):
            if "require_comparison_protocol" in ni:
                force_cross = bool(ni.get("require_comparison_protocol"))
            if "require_hetero_alignment" in ni:
                force_hetero = bool(ni.get("require_hetero_alignment"))
        report = assess_comparison_protocol(
            plan=inputs["plan"],
            prereg=inputs["prereg"],
            overview=inputs["overview"],
            claim_texts=inputs["claim_texts"],
            force_cross_task=force_cross,
            force_hetero=force_hetero,
        )
        passed = bool(report["passed"])
        reasoning = str(report["reason"])

    elif name == "resource_feasibility":
        inputs = collect_resource_feasibility_inputs(state)
        force = None
        ni = state.hook_state.get("node_inputs") or {}
        if isinstance(ni, dict) and "require_resource_feasibility" in ni:
            force = bool(ni.get("require_resource_feasibility"))
        report = assess_resource_feasibility(
            plan=inputs["plan"],
            prereg=inputs["prereg"],
            overview=inputs["overview"],
            claim_texts=inputs["claim_texts"],
            force=force,
        )
        passed = bool(report["passed"])
        reasoning = str(report["reason"])

    elif name == "cost_instrumentation":
        inputs = collect_cost_instrumentation_inputs(state)
        force = None
        ni = state.hook_state.get("node_inputs") or {}
        if isinstance(ni, dict) and "require_cost_instrumentation" in ni:
            force = bool(ni.get("require_cost_instrumentation"))
        report = assess_cost_instrumentation(
            plan=inputs["plan"],
            prereg=inputs["prereg"],
            overview=inputs["overview"],
            claim_texts=inputs["claim_texts"],
            force=force,
        )
        passed = bool(report["passed"])
        reasoning = str(report["reason"])

    elif name == "experiment_results_accounted":
        unaccounted = unaccounted_experiments(state)
        if not unaccounted:
            passed = True
            reasoning = "experiment 的结果产物都已在 research_state 里有说法"
        else:
            reasoning = (
                "以下 experiment 结果产物在 research_state 里只字未提："
                + ", ".join(unaccounted[:6])
                + (f"（共 {len(unaccounted)} 份）" if len(unaccounted) > 6 else "")
                + "。裁决层的义务是给出说法 —— 裁决、记 inconclusive、或写进 "
                "gaps/next_steps 说明为什么不用它；当它没发生不行。"
            )

    elif name == "adjudication_evidence_resolvable":
        bad = unresolvable_adjudication_evidence(state)
        if not bad:
            passed = True
            reasoning = "所有 supported/refuted 裁决的 evidence 都指向真实产物或 run"
        else:
            reasoning = (
                "裁决的 evidence 指不到任何真实产物："
                + "；".join(
                    f"{row['id']}({row['status']}) → {', '.join(row['unresolvable'][:3])}"
                    for row in bad[:4]
                )
                + "。填 experiment 产物 id 或 run id；指不到东西的证据不算证据。"
            )

    return {"name": name, "passed": passed, "reasoning": reasoning}


async def _validate_hypothesis_outputs(
    state: State,
    checks: list[str] | None = None,
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    # check 名的合法集由 parameters_schema 的 items.enum 声明，派发口核取值。
    names = list(checks) if checks else list(_CHECK_NAMES)

    mode = current_mode(state)
    authored = run_authored_commitments(state)
    skipped = _applicability(state)

    results: list[dict[str, Any]] = []
    for n in names:
        if n in skipped:
            results.append({
                "name": n,
                "passed": True,
                "applicable": False,
                "reasoning": "n/a —— " + skipped[n],
            })
            continue
        row = _run_check(n, state)
        row["applicable"] = True
        results.append(row)

    for r in results:
        # research_questions_declared 曾按 authored 分档（新写 prereg 才
        # blocking）——判决拆除批 3w 后整条入 _ADVISORY_CHECKS，分档不再需要。
        advisory = r["name"] in _ADVISORY_CHECKS
        r["tier"] = "advisory" if advisory else "blocking"
    failed = [r["name"] for r in results
              if not r["passed"] and r["tier"] == "blocking"]
    advisory_failed = [r["name"] for r in results
                       if not r["passed"] and r["tier"] == "advisory"]
    passed_all = len(failed) == 0

    report_body = {
        "passed": passed_all,
        "mode": mode,
        "authored_new_commitments": authored,
        "n_checks": len(results),
        "n_skipped": len(skipped),
        "n_failed": len(failed),
        "failed_checks": failed,
        "advisory_failures": advisory_failed,
        "not_applicable": skipped,
        "results": results,
    }

    artifact_id: str | None = None
    if save_report:
        lines = [
            "# Hypothesis Output Validation",
            "",
            f"- **passed**（科学凭据层）: {passed_all}",
            f"- mode: `{mode}`（本轮{'有' if authored else '无'}新增科学承诺）",
            f"- checks: {len(results)}",
            f"- blocking failed: {len(failed)}",
            f"- not applicable: {len(skipped)}",
            f"- advisory failed: {len(advisory_failed)}"
            + ("（不拦完成；随本报告交 reviewer 作修订项）" if advisory_failed else ""),
            "",
            "## Results",
        ]
        for r in results:
            if not r.get("applicable", True):
                mark = "–"
            else:
                mark = "✓" if r["passed"] else ("△" if r["tier"] == "advisory" else "✗")
            lines.append(f"- {mark} **{r['name']}** [{r['tier']}]: {r['reasoning']}")
        content = (
            "\n".join(lines)
            + "\n\n---\n\n```json\n"
            + json.dumps(report_body, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
        summary_flags = {
            "hypothesis_output_validation_passed": passed_all,
            **{r["name"]: bool(r["passed"]) for r in results},
        }
        saved = save_hypothesis_singleton(
            state,
            "hypothesis_output_validation",
            "Output_Validation",
            content,
            metadata={
                "passed": passed_all,
                "mode": mode,
                "authored_new_commitments": authored,
                "n_failed": len(failed),
                "failed_checks": failed,
                "advisory_failures": advisory_failed,
                "not_applicable": sorted(skipped),
                "summary_flags": summary_flags,
            },
        )
        artifact_id = saved["id"]

    if passed_all:
        msg = (
            f"✅ 节点内自检通过（mode={mode}；"
            f"{len(results) - len(skipped)} 项已审，{len(skipped)} 项本轮不适用）。"
        )
    else:
        msg = (
            f"⚠️ {len(failed)} 项**科学凭据**未通过（{', '.join(failed)}），"
            "请修复后重新调用本工具再结束 run。"
        )
    if advisory_failed:
        msg += (
            f"\n△ 另有 {len(advisory_failed)} 项结构质量问题"
            f"（{', '.join(advisory_failed)}）—— 不拦完成；顺手能修就修，"
            "修不动会随验证报告交给 reviewer 作修订项。"
        )

    return {
        "status": "success",
        "passed": passed_all,
        "mode": mode,
        "results": results,
        "failed_checks": failed,
        "advisory_failures": advisory_failed,
        "not_applicable": skipped,
        "artifact_id": artifact_id,
        "message": msg,
    }


register_tool(
    ToolDefinition(
        name="validate_hypothesis_outputs",
        description=(
            "hypothesis 节点**内部成果自检**（纯代码）。\n\n"
            "结果写入 `hypothesis_output_validation`；框架 quality_check "
            "`hypothesis_output_validation_passed` 机械读 metadata.passed——"
            "**passed=false 或未调用 → 节点 incomplete**（即使四类 required artifact 已齐）。\n\n"
            "**Use when**（结束 run 前必调）：\n"
            "  - pre_registration（承诺写在这里，已冻结）/ research_plan / HIF 都完成后\n"
            "  - 想确认 prereg freeze、falsifier、scope、五件套、computational_workflow、"
            "结论审核、**用户多支柱覆盖（防退化）**、**分类定义保真**、"
            "**阈值依据（threshold_grounding）**、**跨任务比较协议**、"
            "**资源可执行性（resource_feasibility）**、"
            "**成本采集（cost_instrumentation）**是否齐全\n\n"
            "**返回**：逐项 passed/failed + hypothesis_output_validation artifact。\n"
            "passed=false → 按 failed_checks 修复后重跑，不要直接结束。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "checks": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(_CHECK_NAMES)},
                    "description": (
                        "要跑的 check 名列表；留空跑全部"
                        "（含 user_goal_coverage / definition_fidelity / "
                        "threshold_grounding / comparison_protocol_complete / "
                        "resource_feasibility / cost_instrumentation）"
                    ),
                },
                "save_report": {"type": "boolean", "default": True},
            },
        },
        allowed_node_types=["hypothesis"],
    ),
    _validate_hypothesis_outputs,
)
