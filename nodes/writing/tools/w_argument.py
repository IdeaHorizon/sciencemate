"""Manuscript argument and evidence placement; never changes research records."""

from __future__ import annotations

import json


def argument_guidance(document_kind: str) -> str:
    common = (
        "显式多问与必达项以用户请求为准；聚焦不能授权删去用户要求。必要且相互依赖的子问可以共同支撑论证，"
        "不规定问题数量，也不规定每题独立一节。这里组织已经取得的成果，不重新制定研究计划、改写科学结论或删除原证据。"
    )
    if document_kind == "project_summary":
        return (
            "项目总结：按用户要求逐项回顾项目工作、成果、证据和适用边界；不能套用单篇研究论文的选材标准，"
            "把独立成果静默移出总结。可以概括共同认识，同时保留各项工作的可追溯位置。" + common
        )
    return (
        "研究论文：明确本文要回答的问题和最有证据的发现，说明哪些方法、结果及相依子问对这条论证必要。"
        "可以围绕一个主问题，也可以保留用户明确要求或论证所需的多个问题。独立成果可以安排在附录、项目报告或后续研究，"
        "但必须记录去向与理由，原材料仍留在项目。证明、反例、数据表和图都是证据形式，按读者需要选择。"
        + common
    )


def argument_context(brief: dict, outline: dict | None) -> str:
    plan = outline or {}
    kind = str(
        brief.get("document_kind")
        or ("project_summary" if brief.get("genre") == "project_summary" else "research_article")
    )
    rows = ["# 本稿的论证与选材", argument_guidance(kind)]
    if brief.get("source_request"):
        rows.append("## 原写作请求（不得用选材声明覆盖）\n" + str(brief["source_request"]))
    if brief.get("required_questions"):
        rows.append(
            "## 用户明确要求的问题\n" + json.dumps(brief["required_questions"], ensure_ascii=False)
        )
    focus = plan.get("editorial_focus")
    if focus:
        rows.append(
            "## 全稿主张与材料去向（project_only/future 的材料不据此编入正文）\n"
            + json.dumps(focus, ensure_ascii=False)
        )
    else:
        rows.append(
            "旧提纲未单列选材声明：继续依照用户请求与现有主张写作，不凭模板新增问题或扩展研究范围。"
        )
    if plan.get("question_coverage"):
        rows.append(
            "## 明确问题的稿件位置\n" + json.dumps(plan["question_coverage"], ensure_ascii=False)
        )
    return "\n\n".join(rows)


def focus_problems(brief: dict, outline: dict, known_ids: set[str]) -> list[str]:
    """Report malformed source links/declared coverage, not scientific sufficiency."""
    problems = []
    parts = [part for part in outline.get("parts") or [] if isinstance(part, dict)]
    part_ids = {str(part.get("id")) for part in parts}
    placements = {}
    focus = outline.get("editorial_focus")
    if focus is not None and not isinstance(focus, dict):
        problems.append("editorial_focus 必须是对象；旧提纲可不填，不能以格式字段重建研究任务")
    elif isinstance(focus, dict):
        selections = focus.get("evidence_selection") or []
        if not isinstance(selections, list):
            problems.append("editorial_focus.evidence_selection 必须是列表")
            selections = []
        for selection in selections:
            if not isinstance(selection, dict):
                problems.append("editorial_focus.evidence_selection 的每项必须是对象")
                continue
            if selection.get("placement") not in {"body", "appendix", "project_only", "future"}:
                problems.append(
                    "材料 placement 只能是 body/appendix/project_only/future；"
                    "只记录选材，不移动原件"
                )
            evidence = selection.get("evidence") or []
            if not isinstance(evidence, list):
                problems.append("选材 evidence 必须是卷宗 id 列表")
                continue
            for identity in evidence:
                if not isinstance(identity, str):
                    problems.append("选材 evidence 的每项必须是卷宗 id 字符串")
                    continue
                if identity not in known_ids and identity != "user_prompt":
                    problems.append(f"选材 evidence id {identity!r} 不在卷宗里")
                placements[identity] = selection.get("placement")
        for part in parts:
            for subsection in part.get("subsections") or [part]:
                for claim in subsection.get("claims") or []:
                    for identity in claim.get("evidence") or []:
                        placement = placements.get(identity)
                        if placement in {"project_only", "future"} or (
                            placement == "appendix" and part.get("id") != "appendix"
                        ):
                            problems.append(
                                f"{part.get('id')}: evidence {identity} 的选材去向是 {placement}，"
                                "与实际主张位置冲突；修正声明或提纲，不移动原证据"
                            )
    coverage = outline.get("question_coverage") or {}
    for question in brief.get("required_questions") or []:
        identity = question["id"]
        entry = coverage.get(identity) if isinstance(coverage, dict) else None
        placement = entry.get("placement") if isinstance(entry, dict) else None
        if placement not in {"body", "appendix"}:
            problems.append(
                f"用户明确要求的 {identity} 未安排在 body/appendix；"
                "聚焦不能静默省略，须保留回答或先取得范围变更"
            )
        elif entry.get("part") not in part_ids or (
            placement == "appendix" and entry.get("part") != "appendix"
        ):
            problems.append(
                f"用户明确要求的 {identity} 须指向实际提纲部件 part；"
                "附录回答须有 appendix 部件，不能只留位置承诺"
            )
    return problems
