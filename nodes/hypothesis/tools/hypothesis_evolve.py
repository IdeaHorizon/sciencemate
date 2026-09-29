"""evolve_hypothesis — 对候选假设做 Evolution 式 refine 建议（Co-Scientist Evolution agent 轻量版）。"""
from __future__ import annotations

import json
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

_EVOLUTION_STRATEGIES: dict[str, dict[str, str]] = {
    "synthesize": {
        "title": "Synthesize — 合成多条路径",
        "prompt": (
            "将当前假设与 scratchpad 中其它候选的机制路径合并："
            "保留核心 falsifiable prediction，引入第二机制作为 moderator/interaction。"
        ),
        "example": "H1(孔隙效应) + H2(拓扑效应) → 「孔隙尺寸与节点面带隙在 X 阈值以上产生协同，否则解耦」",
    },
    "simplify": {
        "title": "Simplify —  sharpen claim 与 falsifier",
        "prompt": (
            "缩短 claim_text 到一句核心因果；"
            "把 falsification_criteria_structured 的 metric/threshold 改 sharper（单一主指标）；"
            "**改数值 threshold 时必须同步更新 threshold_rationale**（文献/理论/先导），禁止拍脑袋比例；"
            "定性/存在性证伪用 comparison=qualitative|exists|not_exists，写清判据即可。"
        ),
        "example": "去掉多重归因，只保留一个可机械判定的 primary metric",
    },
    "regime_shift": {
        "title": "Regime shift — 加 boundary condition",
        "prompt": (
            "为假设加入明确的 scope regime（温度/密度/数据集 split），"
            "使预测在特定条件下非显然、在条件外可 falsify。"
        ),
        "example": "「在 T≥350K 且 ρ>0.8 时 …；T<300K 时预测失效」",
    },
    "analogy": {
        "title": "Analogy — 跨域类比迁移",
        "prompt": (
            "从 scratchpad 跨域路径 (D) 引入类比机制，"
            "替换原假设中 phenomenological 部分为可检验的中间机制。"
        ),
        "example": "借鉴 RL exploration / 材料相变 / 其它领域已验证机制",
    },
    "assumption_repair": {
        "title": "Assumption repair — 修复 non-fundamental 错误",
        "prompt": (
            "根据 deep verification：若 Reflection 标记某 sub-assumption 错误但 non-fundamental，"
            "替换该 assumption 并重写 claim，保留核心 hypothesis 骨架。"
        ),
        "example": "错误实验协议细节 → 改 protocol；核心机制主张保留",
    },
}


def _build_evolution_plan(
    hypothesis: dict[str, Any],
    mode: str,
    *,
    audit_feedback: str = "",
    hif_feedback: str = "",
    merge_with: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # mode 的合法值由 parameters_schema 的 enum 声明，派发口核取值并列出合法值。
    strategy = _EVOLUTION_STRATEGIES[mode]

    label = hypothesis.get("label") or "H?"
    claim = hypothesis.get("claim_text") or hypothesis.get("hypothesis_text") or ""
    assumptions = hypothesis.get("assumption_tree") or []

    actions: list[str] = [strategy["prompt"]]
    if audit_feedback:
        actions.append(f"Address audit: {audit_feedback}")
    if hif_feedback:
        actions.append(f"Address HIF: {hif_feedback}")
    if merge_with and mode == "synthesize":
        other = merge_with.get("claim_text") or merge_with.get("hypothesis_text") or ""
        actions.append(f"Merge with {merge_with.get('label', '?')}: {other[:200]}")

    fundamental = [a for a in assumptions if a.get("fundamental")]
    if assumptions and mode == "assumption_repair":
        actions.append(
            f"Fundamental assumptions to preserve: "
            f"{[a.get('text', '')[:80] for a in fundamental]}"
        )

    return {
        "label": label,
        "mode": mode,
        "strategy": strategy["title"],
        "original_claim": claim[:500],
        "evolution_actions": actions,
        "example": strategy["example"],
        "rewrite_checklist": [
            "新 claim 必须与原文有 ≥1 个非显然差异（新 regime / metric / mechanism）",
            "**禁止**改写用户 locked_definitions 的类别名/定义/互斥关系；只能改预测与 falsifier",
            "不得合并/拆分/重命名操作类别；要改 ontology → request_human_input",
            "文献 taxonomy 仅可作对照，不得替换用户分类表",
            "改数值 threshold 必须同步写 threshold_rationale（source_type + citation_or_derivation + scientific_meaning）；"
            "定性/存在性用 comparison=qualitative|exists|not_exists + 清晰判据",
            "禁止无依据的比例阈值（如随意 +20% / 2×）；无依据则改相对效应量、加 pilot 或 request_human_input",
            "重写后必须再调 audit_hypothesis_vs_conclusions + audit_definition_fidelity + audit_threshold_grounding + audit_resource_feasibility + audit_cost_instrumentation",
            "若改了 prediction，更新 falsification_criteria_structured",
            "若合成两条，确保 scope 正交或 interaction 可检验",
        ],
    }


async def _evolve_hypothesis(
    state: State,
    hypothesis: dict[str, Any],
    mode: str = "simplify",
    audit_feedback: str = "",
    hif_feedback: str = "",
    merge_with: dict[str, Any] | None = None,
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    # hypothesis.claim_text 的嵌套 required 注册表校验器不查，这里仍是唯一防线。
    claim = (hypothesis.get("claim_text") or hypothesis.get("hypothesis_text") or "").strip()
    if not claim:
        return {"status": "error", "error": "hypothesis 需含 claim_text"}

    plan = _build_evolution_plan(
        hypothesis,
        mode,
        audit_feedback=audit_feedback,
        hif_feedback=hif_feedback,
        merge_with=merge_with,
    )

    rounds = state.hook_state.setdefault("hypothesis_evolution_log", [])
    if isinstance(rounds, list):
        rounds.append(plan)

    artifact_id: str | None = None
    if save_report:
        md = [
            f"# Evolution Plan — {plan['label']} ({plan['mode']})",
            "",
            f"**Strategy**: {plan['strategy']}",
            "",
            f"**Original**: {plan['original_claim']}",
            "",
            "## Actions",
        ]
        for a in plan["evolution_actions"]:
            md.append(f"- {a}")
        md.extend(["", "## Rewrite checklist"])
        for c in plan["rewrite_checklist"]:
            md.append(f"- [ ] {c}")
        md.append(f"\n**Example**: {plan['example']}")
        content = (
            "\n".join(md)
            + "\n\n---\n\n```json\n"
            + json.dumps(plan, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
        art = state.save_artifact(
            "hypothesis_evolution_plan",
            f"Evolve_{plan['label']}_{mode}",
            content,
            metadata={"label": plan["label"], "mode": mode},
        )
        artifact_id = art["id"]

    return {
        "status": "success",
        "plan": plan,
        "artifact_id": artifact_id,
        "available_modes": list(_EVOLUTION_STRATEGIES.keys()),
        "message": (
            f"Evolution plan ({mode}) for {plan['label']}。"
            "按 evolution_actions 重写 claim，然后 re-audit + re-score。"
        ),
    }


register_tool(
    ToolDefinition(
        name="evolve_hypothesis",
        description=(
            "为候选 hypothesis 生成 Evolution 式 refine 计划（Co-Scientist Evolution 轻量版）。\n\n"
            "**Use when**：\n"
            "  - audit 有 minor flag 或 HIF 25–44，值得 refine 而非丢弃\n"
            "  - 两条高 HIF 候选机制互补，需 synthesize\n"
            "  - deep verification 标记 non-fundamental assumption 错误\n\n"
            "**mode**: synthesize | simplify | regime_shift | analogy | assumption_repair\n\n"
            "**注意**：本工具产出 rewrite 计划，不直接改 KB；agent 按计划重写后再 audit。\n"
            "**红线**：不得改写用户 locked_definitions（类别名/定义）；只能 sharpen 预测与 falsifier。\n"
            "改数值 threshold 必须同步写 threshold_rationale，禁止无依据比例阈值；"
            "定性/存在性勿硬编伪精确数字。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "hypothesis": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "claim_text": {"type": "string"},
                        "assumption_tree": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "fundamental": {"type": "boolean"},
                                },
                            },
                        },
                    },
                    "required": ["claim_text"],
                },
                "mode": {
                    "type": "string",
                    "enum": list(_EVOLUTION_STRATEGIES.keys()),
                    "default": "simplify",
                },
                "audit_feedback": {"type": "string", "default": ""},
                "hif_feedback": {"type": "string", "default": ""},
                "merge_with": {
                    "type": "object",
                    "description": "synthesize 模式下的第二条候选",
                },
                "save_report": {"type": "boolean", "default": True},
            },
            "required": ["hypothesis"],
        },
        allowed_node_types=["hypothesis"],
    ),
    _evolve_hypothesis,
)
