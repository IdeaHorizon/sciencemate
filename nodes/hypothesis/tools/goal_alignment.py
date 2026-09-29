"""audit_user_goal_alignment — 防止复杂诉求退化成单一小点。

多支柱 user_prompt（must_cover_themes）必须在最终 pre_registration +
research_plan 合集中全部覆盖；research_plan 单独也须覆盖（避免只在假设里
点名、实验设计却只做窄点）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from .artifact_save import save_hypothesis_singleton
from .dialogue_context import (
    build_dialogue_context,
    extract_must_cover_from_inputs,
    extract_must_cover_themes,
)
from .research_goal import parse_research_goal

# Optional English / near-synonym anchors for common CS pillars
_THEME_ALIASES: dict[str, tuple[str, ...]] = {
    "任务拆分": ("task decomposition", "task split", "task routing", "任务分解", "拆分任务"),
    "动态路由": ("dynamic routing", "routing policy", "router", "路由策略"),
    "多模型执行": ("multi-model", "multi model", "model ensemble", "多模型", "模型选择"),
    "成本分析": ("cost analysis", "cost estimate", "token cost", "成本", "费用"),
    "步骤分类": ("step classification", "action taxonomy", "步骤类别"),
    "验证等级": ("verification level", "validation tier", "校验等级", "验证级别"),
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _theme_tokens(theme: str) -> list[str]:
    """CJK chars as 2-grams + latin words ≥3."""
    t = theme.strip()
    tokens: list[str] = []
    latin = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", t)
    tokens.extend(w.lower() for w in latin)
    cjk = re.findall(r"[\u4e00-\u9fff]+", t)
    for span in cjk:
        if len(span) <= 2:
            tokens.append(span)
        else:
            tokens.extend(span[i : i + 2] for i in range(len(span) - 1))
            tokens.append(span)
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def theme_matched(theme: str, corpus_norm: str) -> bool:
    """True if theme (or aliases / token evidence) appears in normalized corpus."""
    if not theme or not corpus_norm:
        return False
    t_norm = _norm(theme)
    if len(t_norm) >= 2 and t_norm in corpus_norm:
        return True
    for alias in _THEME_ALIASES.get(theme, ()):
        if _norm(alias) and _norm(alias) in corpus_norm:
            return True
    tokens = _theme_tokens(theme)
    if not tokens:
        return False
    # Require majority of tokens (≥2 if many) so partial collapse still fails
    hits = sum(1 for tok in tokens if _norm(tok) in corpus_norm)
    need = 1 if len(tokens) == 1 else max(2, (len(tokens) + 1) // 2)
    return hits >= need


def _read_latest_content(state: State, artifact_type: str) -> str:
    arts = state.list_artifacts(artifact_type)
    if not arts:
        return ""
    rec = state.read_artifact(arts[-1]["id"]) or {}
    return str(rec.get("content") or "")


def resolve_must_cover_themes(state: State) -> list[str]:
    """Reuse get_research_goal cache, else rebuild from node_inputs / dialogue."""
    cached = state.hook_state.get("research_goal_parsed")
    if isinstance(cached, dict):
        themes = cached.get("must_cover_themes")
        if isinstance(themes, list) and themes:
            return [str(t) for t in themes if str(t).strip()]

    inputs = state.hook_state.get("node_inputs") or {}
    if not isinstance(inputs, dict):
        inputs = {}
    dialogue = state.hook_state.get("dialogue_context")
    if not isinstance(dialogue, dict):
        dialogue = build_dialogue_context(state, inputs)
    parsed = parse_research_goal(inputs, dialogue=dialogue)
    themes = parsed.get("must_cover_themes") or []
    if themes:
        return [str(t) for t in themes if str(t).strip()]

    # Last resort: explicit inputs + parse user prompt
    explicit = extract_must_cover_from_inputs(inputs)
    up = dialogue.get("user_prompt") if isinstance(dialogue, dict) else None
    return extract_must_cover_themes(str(up or ""), explicit=explicit)


def assess_goal_coverage(
    themes: list[str],
    *,
    prereg: str,
    plan: str,
    overview: str = "",
) -> dict[str, Any]:
    """Score multi-pillar coverage; fail when complex request collapses to a subset."""
    themes = [t.strip() for t in themes if str(t).strip()]
    plan_norm = _norm(plan)
    prereg_norm = _norm(prereg)
    overview_norm = _norm(overview)
    combined_norm = plan_norm + prereg_norm + overview_norm

    if len(themes) < 2:
        return {
            "applicable": False,
            "passed": True,
            "n_themes": len(themes),
            "covered": themes,
            "missing_in_combined": [],
            "missing_in_plan": [],
            "coverage_ratio": 1.0 if not themes else 1.0,
            "reason": (
                "must_cover_themes < 2：无多支柱退化门禁"
                if len(themes) < 2
                else "单支柱"
            ),
            "per_theme": [
                {
                    "theme": t,
                    "in_plan": theme_matched(t, plan_norm),
                    "in_prereg": theme_matched(t, prereg_norm),
                    "in_combined": theme_matched(t, combined_norm),
                }
                for t in themes
            ],
        }

    per_theme: list[dict[str, Any]] = []
    missing_combined: list[str] = []
    missing_plan: list[str] = []
    for t in themes:
        in_plan = theme_matched(t, plan_norm)
        in_prereg = theme_matched(t, prereg_norm)
        in_combined = theme_matched(t, combined_norm)
        per_theme.append({
            "theme": t,
            "in_plan": in_plan,
            "in_prereg": in_prereg,
            "in_combined": in_combined,
        })
        if not in_combined:
            missing_combined.append(t)
        if not in_plan:
            missing_plan.append(t)

    covered = [p["theme"] for p in per_theme if p["in_combined"]]
    ratio = len(covered) / len(themes)
    # Hard rule: all themes in combined corpus AND all in research_plan
    passed = not missing_combined and not missing_plan
    if not passed:
        parts = []
        if missing_combined:
            parts.append(f"合集缺失: {', '.join(missing_combined)}")
        if missing_plan:
            parts.append(f"research_plan 缺失: {', '.join(missing_plan)}")
        reason = (
            f"任务退化：{len(covered)}/{len(themes)} 支柱被覆盖。"
            + "；".join(parts)
            + "。须补假说/实验设计覆盖全部 must_cover，禁止只做窄点。"
        )
    else:
        reason = f"全部 {len(themes)} 个 must_cover 支柱已在 prereg+plan 覆盖"

    return {
        "applicable": True,
        "passed": passed,
        "n_themes": len(themes),
        "covered": covered,
        "missing_in_combined": missing_combined,
        "missing_in_plan": missing_plan,
        "coverage_ratio": round(ratio, 3),
        "reason": reason,
        "per_theme": per_theme,
    }


async def _audit_user_goal_alignment(
    state: State,
    themes: list[str] | None = None,
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    resolved = [str(t).strip() for t in (themes or []) if str(t).strip()]
    if not resolved:
        resolved = resolve_must_cover_themes(state)

    prereg = _read_latest_content(state, "pre_registration")
    plan = _read_latest_content(state, "research_plan")
    overview = _read_latest_content(state, "hypothesis_research_overview")

    if not plan and not prereg:
        return {
            "status": "error",
            "passed": False,
            "error": "缺少 pre_registration / research_plan，无法做覆盖审计",
            "must_cover_themes": resolved,
        }

    report = assess_goal_coverage(
        resolved, prereg=prereg, plan=plan, overview=overview,
    )

    artifact_id: str | None = None
    if save_report:
        body = {
            "must_cover_themes": resolved,
            **report,
        }
        md_lines = [
            "# User Goal Alignment Audit",
            "",
            f"- **passed**: {report['passed']}",
            f"- applicable: {report['applicable']}",
            f"- coverage_ratio: {report['coverage_ratio']}",
            f"- themes: {', '.join(resolved) or '(none)'}",
            "",
            report["reason"],
            "",
            "## Per theme",
        ]
        for row in report["per_theme"]:
            marks = []
            marks.append("plan✓" if row["in_plan"] else "plan✗")
            marks.append("prereg✓" if row["in_prereg"] else "prereg✗")
            md_lines.append(f"- **{row['theme']}**: {', '.join(marks)}")
        content = (
            "\n".join(md_lines)
            + "\n\n```json\n"
            + json.dumps(body, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
        saved = save_hypothesis_singleton(
            state,
            "hypothesis_goal_alignment",
            "Goal_Alignment",
            content,
            metadata={
                "passed": report["passed"],
                "coverage_ratio": report["coverage_ratio"],
                "n_themes": report["n_themes"],
                "missing_in_plan": report["missing_in_plan"],
            },
        )
        artifact_id = saved["id"]

    state.hook_state["last_goal_alignment"] = {
        "must_cover_themes": resolved,
        **report,
    }

    return {
        "status": "success",
        "passed": report["passed"],
        "must_cover_themes": resolved,
        "artifact_id": artifact_id,
        "message": report["reason"],
        **report,
    }


register_tool(
    ToolDefinition(
        name="audit_user_goal_alignment",
        description=(
            "审计最终产物是否**覆盖用户多支柱诉求**，防止复杂任务退化成单一小点。\n\n"
            "**Use when**：\n"
            "  - save pre_registration（草稿）+ research_plan 之后\n"
            "  - **freeze_artifact 之前**（必调；失败须改未冻结草稿）\n"
            "  - validate_hypothesis_outputs 之前\n\n"
            "读取 get_research_goal 的 must_cover_themes；检查 pre_registration + "
            "research_plan 是否全部覆盖。passed=false → 补假说/扩 workflow，"
            "禁止带着缺口 freeze / 结束。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "themes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "覆盖主题列表；留空则用 get_research_goal 解析结果",
                },
                "save_report": {"type": "boolean", "default": True},
            },
        },
        allowed_node_types=["hypothesis"],
    ),
    _audit_user_goal_alignment,
)
