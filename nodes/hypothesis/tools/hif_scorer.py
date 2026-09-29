"""score_hypothesis_innovation — hypothesis 节点 HIF 创新性评分工具。"""
from __future__ import annotations

import json
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from .artifact_save import save_hypothesis_singleton
from .hif_core import (
    HIFDimensions,
    TIER_LABELS_ZH,
    compute_hif,
    render_report_markdown,
)


#: HIF 五个必填维度 —— 缺哪个要点名说，别让 KeyError 的 repr 冒出去。
_REQUIRED_DIMS = {
    "G": "Generalization 普适性（0-5）",
    "D": "Depth 机理深度（0-5）",
    "M": "Mechanism 机制新颖度（0-5）",
    "P": "Predictive power 预测力（0-5）",
    "R": "Redundancy 与已知的重合度（0-5；N = 5 - R）",
}
_OPTIONAL_DIMS = {
    "Q": "Plausibility 可信度（0-5，可省；Q≤1 直接 plausibility_reject）",
    "I": "Impact 影响力（0-5，可省；给了就走 extended 公式）",
}


def _assess_one(raw: dict[str, Any]) -> dict[str, Any]:
    # 缺 key 时 `int(raw["G"])` 抛 KeyError('G')，上层拼出来是
    # `assessments[0]: 'G'` —— 看着像"值 'G' 非法"，实际是"少了 G 这个键"，
    # 而且从头到尾没说过合法维度有哪些。实测模型三条评估全撞同一句。
    missing = [k for k in _REQUIRED_DIMS if raw.get(k) is None]
    if missing:
        raise ValueError(
            "缺必填维度 " + ", ".join(missing) + "。"
            "五个必填：" + "；".join(f"{k}={v}" for k, v in _REQUIRED_DIMS.items())
            + "。可选：" + "；".join(f"{k}={v}" for k, v in _OPTIONAL_DIMS.items())
        )
    label = raw.get("label") or raw.get("name") or "hypothesis"
    claim_text = raw.get("claim_text") or raw.get("hypothesis_text") or ""
    rationale = raw.get("rationale") or raw.get("notes") or ""
    kb_overlap = raw.get("kb_overlap") or raw.get("kb_overlap_notes") or ""

    q = raw.get("Q")
    i = raw.get("I")
    result = compute_hif(
        HIFDimensions(
            G=int(raw["G"]),
            D=int(raw["D"]),
            M=int(raw["M"]),
            P=int(raw["P"]),
            R=int(raw["R"]),
            Q=int(q) if q is not None else None,
            I=int(i) if i is not None else None,
        )
    )
    entry = result.to_dict()
    entry["label"] = label
    entry["claim_text"] = claim_text
    entry["rationale"] = rationale
    entry["kb_overlap"] = kb_overlap
    return entry


def _build_summary(assessments: list[dict[str, Any]]) -> dict[str, Any]:
    hifs = [a["hif"] for a in assessments]
    max_a = max(assessments, key=lambda x: x["hif"])
    min_a = min(assessments, key=lambda x: x["hif"])
    return {
        "n_assessed": len(assessments),
        "max_hif": max(hifs),
        "min_hif": min(hifs),
        "max_label": max_a.get("label"),
        "min_label": min_a.get("label"),
        "any_transformative": any(a["tier"] == "transformative" for a in assessments),
        "any_paradigm_flag": any(a.get("paradigm_flag") for a in assessments),
        "any_plausibility_reject": any(a.get("plausibility_reject") for a in assessments),
    }


async def _score_hypothesis_innovation(
    state: State,
    assessments: list[dict[str, Any]],
    report_name: str = "HIF_Assessment",
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    # assessments 非空由 parameters_schema 的 minItems=1 声明，派发口核取值。
    # 逐条的必填维度（items.required）注册表校验器不查嵌套 required，
    # 下面 _assess_one 的检查仍是唯一防线，保留。
    scored: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, raw in enumerate(assessments):
        try:
            scored.append(_assess_one(raw))
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"assessments[{i}]: {e}")

    if errors:
        return {"status": "error", "error": "; ".join(errors), "partial": scored}

    summary = _build_summary(scored)
    report_body = {
        "formula": (
            "legacy: HIF = round(100*(0.25G+0.20D+0.25M+0.20P+0.10N)/5); "
            "extended (+I): round(100*(0.22G+0.18D+0.22M+0.18P+0.10N+0.10I)/5); "
            "Q≤1 → plausibility_reject"
        ),
        "summary": summary,
        "assessments": scored,
    }
    markdown = render_report_markdown(scored, summary)
    content = markdown + "\n---\n\n```json\n" + json.dumps(report_body, indent=2, ensure_ascii=False) + "\n```\n"

    artifact_id: str | None = None
    if save_report:
        saved = save_hypothesis_singleton(
            state,
            "hypothesis_innovation_report",
            report_name,
            content,
            metadata={
                "hif_summary": summary,
                "n_assessed": summary["n_assessed"],
                "max_hif": summary["max_hif"],
                "min_hif": summary["min_hif"],
            },
        )
        artifact_id = saved["id"]

    state.hook_state["hif_last_report"] = report_body

    lines = [
        f"✅ HIF 评估完成：{summary['n_assessed']} 条假设",
        f"   最高 HIF={summary['max_hif']} ({summary['max_label']}, "
        f"{TIER_LABELS_ZH.get(max(scored, key=lambda x: x['hif'])['tier'], '?')})",
        f"   最低 HIF={summary['min_hif']} ({summary['min_label']})",
    ]
    if summary["any_paradigm_flag"]:
        lines.append("   ⚠️ 存在 paradigm_flag=true —— 需 extra falsifiability 审查")
    if summary.get("any_plausibility_reject"):
        lines.append("   ⚠️ 存在 plausibility_reject (Q≤1) —— 必须 evolve 或丢弃，不可 prereg")
    if artifact_id:
        lines.append(f"   报告 artifact: {artifact_id}")

    return {
        "status": "success",
        "assessments": scored,
        "summary": summary,
        "artifact_id": artifact_id,
        "message": "\n".join(lines),
    }


register_tool(
    ToolDefinition(
        name="score_hypothesis_innovation",
        description=(
            "按 HIF 公式量化科学假设的创新性，并保存 hypothesis_innovation_report artifact。\n\n"
            "**Use when**（hypothesis 节点必调）：\n"
            "  - 候选 hypothesis 已起草，准备写 pre_registration / create_claim 之前\n"
            "  - 需比较 2+ 条假设的创新程度，决定保留哪些\n"
            "  - 按 skill `hypothesis_novelty_assessment` 完成 search_kb 冗余检查后\n\n"
            "**Do NOT use when**：\n"
            "  - prereg 已 freeze（假设已锁定）\n"
            "  - 还没查 KB / survey open_questions 就打分\n\n"
            "**流程**：先 search_kb 估 R → 按 rubric 填 G/D/M/P/R/Q/I（各 0–5 整数；Q/I 推荐）"
            "→ 本工具算 HIF + tier → 自动写 hypothesis_innovation_report。\n"
            "**Q≤1 → plausibility_reject，不可 prereg。**\n\n"
            "**返回**：每条假设的 hif / tier / paradigm_flag + summary + artifact_id。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "assessments": {
                    "type": "array",
                    "description": "每条候选 hypothesis 的 HIF 维度评分",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "假设标签，如 H1 / H2",
                            },
                            "claim_text": {
                                "type": "string",
                                "description": "假设陈述（用于报告可读性）",
                            },
                            "G": {
                                "type": "integer",
                                "description": "Gap 覆盖度 0–5",
                                "minimum": 0,
                                "maximum": 5,
                            },
                            "D": {
                                "type": "integer",
                                "description": "概念偏离度 0–5",
                                "minimum": 0,
                                "maximum": 5,
                            },
                            "M": {
                                "type": "integer",
                                "description": "机制新颖性 0–5",
                                "minimum": 0,
                                "maximum": 5,
                            },
                            "P": {
                                "type": "integer",
                                "description": "预测非显然性 0–5",
                                "minimum": 0,
                                "maximum": 5,
                            },
                            "R": {
                                "type": "integer",
                                "description": "KB 冗余度 0–5（N=5-R）",
                                "minimum": 0,
                                "maximum": 5,
                            },
                            "Q": {
                                "type": "integer",
                                "description": "Plausibility 0–5（≤1 触发 plausibility_reject）",
                                "minimum": 0,
                                "maximum": 5,
                            },
                            "I": {
                                "type": "integer",
                                "description": "Impact 0–5（对 open question 推进程度）",
                                "minimum": 0,
                                "maximum": 5,
                            },
                            "kb_overlap": {
                                "type": "string",
                                "description": "KB 重叠说明，如 claim_abc123 partial overlap",
                            },
                            "rationale": {
                                "type": "string",
                                "description": "各维度打分的简短理由",
                            },
                        },
                        "required": ["label", "G", "D", "M", "P", "R"],
                    },
                    "minItems": 1,
                },
                "report_name": {
                    "type": "string",
                    "description": "hypothesis_innovation_report artifact 名称 slug，默认 HIF_Assessment",
                },
                "save_report": {
                    "type": "boolean",
                    "description": "是否保存 hypothesis_innovation_report artifact，默认 true",
                },
            },
            "required": ["assessments"],
        },
        allowed_node_types=["hypothesis"],
    ),
    _score_hypothesis_innovation,
)
