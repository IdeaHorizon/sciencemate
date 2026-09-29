"""get_research_goal — 解析结构化 research goal + 用户 prompt / 对话 grounding。"""
from __future__ import annotations

from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from .dialogue_context import build_dialogue_context, format_dialogue_brief

_GOAL_FIELDS = ("title", "goals", "preferences", "constraints", "desirable_attributes")


def _as_list(val: Any) -> list[Any]:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    return [val]


def _merge_unique(dst: list[Any], extras: list[Any]) -> list[Any]:
    out = list(dst)
    seen = {str(x).strip().lower() for x in out if str(x).strip()}
    for item in extras:
        text = str(item).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _opt_int(value: object) -> int | None:
    """调用方给了数就用它，没给就是 None —— **不替调用方发明数字**。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_research_goal(
    node_inputs: dict[str, Any],
    dialogue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize node_inputs (+ optional dialogue) into structured research goal."""
    explicit = node_inputs.get("research_goal")
    if isinstance(explicit, dict):
        goal = {k: explicit.get(k) for k in _GOAL_FIELDS if explicit.get(k) is not None}
        source = "research_goal"
    else:
        goal = {}
        source = "legacy"

    if node_inputs.get("research_question"):
        if "goals" not in goal:
            rq = node_inputs["research_question"]
            goal["goals"] = [rq.strip()] if isinstance(rq, str) else rq
        if "title" not in goal and isinstance(node_inputs["research_question"], str):
            first_line = node_inputs["research_question"].strip().split("\n")[0]
            goal["title"] = first_line[:120]

    if node_inputs.get("scope_hint") and "constraints" not in goal:
        goal["constraints"] = [node_inputs["scope_hint"].strip()]

    if node_inputs.get("primary_paper_doi"):
        goal.setdefault("preferences", [])
        prefs = goal["preferences"]
        if isinstance(prefs, list):
            doi_pref = f"primary_paper_doi: {node_inputs['primary_paper_doi']}"
            if doi_pref not in prefs:
                prefs.append(doi_pref)

    refs = node_inputs.get("reference_papers")
    if refs and isinstance(refs, list):
        goal.setdefault("preferences", [])
        prefs = goal["preferences"]
        if isinstance(prefs, list):
            for p in refs:
                if isinstance(p, dict) and p.get("doi"):
                    entry = f"reference: {p['doi']}"
                    if entry not in prefs:
                        prefs.append(entry)

    dialogue = dialogue or {}
    user_prompt = dialogue.get("user_prompt")
    if isinstance(user_prompt, str) and user_prompt.strip():
        # Prefer explicit user prompt as primary goal when goals empty / legacy
        goals = _as_list(goal.get("goals"))
        if not goals:
            goal["goals"] = [user_prompt.strip()]
        elif user_prompt.strip() not in {str(g).strip() for g in goals}:
            # Keep user prompt first so generation anchors on the live request
            goal["goals"] = [user_prompt.strip(), *goals]
        if not goal.get("title"):
            goal["title"] = user_prompt.strip().split("\n")[0][:120]
        if source == "legacy" and dialogue.get("user_prompt_source") == "conversation":
            source = "dialogue"

    inferred = dialogue.get("inferred_constraints") or []
    if inferred:
        goal["constraints"] = _merge_unique(_as_list(goal.get("constraints")), list(inferred))

    must_cover = list(dialogue.get("must_cover_themes") or [])
    if must_cover:
        # Promote pillars into goals so downstream scoring / prompts see them
        goal["goals"] = _merge_unique(must_cover, _as_list(goal.get("goals")))

    locked_definitions = list(dialogue.get("locked_definitions") or [])
    locked_labels = list(dialogue.get("locked_labels") or [])
    if not locked_labels and locked_definitions:
        locked_labels = [
            str(e.get("label"))
            for e in locked_definitions
            if isinstance(e, dict) and e.get("label")
        ]

    iteration = node_inputs.get("hypothesis_iteration") or {}
    if not isinstance(iteration, dict):
        iteration = {}

    anti_collapse = (
        [
            f"禁止任务退化：must_cover_themes={must_cover} 须在最终假说集合"
            f"+ research_plan 中全部覆盖，不得只做其中 1 个小点",
        ]
        if len(must_cover) >= 2
        else [
            "禁止把复杂用户诉求收窄成单一可证伪小点；若 user_prompt 含多子目标须全覆盖",
        ]
    )
    anti_rewrite = (
        [
            f"禁止改写用户分类：locked_labels={locked_labels} 的名称与定义只读；"
            "prereg/plan 必须原样引用，不得重命名/合并/用文献 taxonomy 替换",
        ]
        if locked_labels
        else []
    )

    return {
        "source": source,
        "goal": goal,
        # ⚠️ 「该提几个问题」**没有默认值**，框架不许报数。
        #
        # 实测（2026-08-16 英国饮食 A/B 跑）：这里原本默认 target_prereg=3，
        # 而它被 `format_goal_brief` 渲染进节点第 0 轮看到的第一条 briefing。
        # 模型于是不多不少交了 3 个问题 —— 那不是课题需要 3 个，是它被点了 3。
        #
        # 问题数只有一个合法来源：**用户诉求拆出来是几个就是几个**。框架在任何
        # 位置（默认值 / 提示词 / 开局注入）报出一个具体数字，那个数字就会变成锚。
        # 调用方**自己**指定了预算时原样转达，没指定就是 None，briefing 里明说
        # 「由课题决定」。
        "iteration": {
            "max_refine_rounds": int(iteration.get("max_refine_rounds", 2)),
            "min_candidates": _opt_int(iteration.get("min_candidates")),
            "target_prereg": _opt_int(iteration.get("target_prereg")),
            "stop_hif_min": int(iteration.get("stop_hif_min", 45)),
        },
        "has_structured_goal": source == "research_goal",
        "user_prompt": user_prompt,
        "must_cover_themes": must_cover,
        "locked_definitions": locked_definitions,
        "locked_labels": locked_labels,
        "dialogue_context": {
            "user_prompt_source": dialogue.get("user_prompt_source"),
            "must_cover_themes": must_cover,
            "locked_definitions": locked_definitions,
            "locked_labels": locked_labels,
            "recent_user_turns": dialogue.get("recent_user_turns") or [],
            "recent_dialogue": dialogue.get("recent_dialogue") or [],
            "inferred_constraints": inferred,
            "conversation_path": dialogue.get("conversation_path"),
            "n_conversation_messages": dialogue.get("n_conversation_messages", 0),
        },
        "checklist": [
            *(dialogue.get("grounding_checklist") or []),
            *anti_collapse,
            *anti_rewrite,
            "最终 hypothesis 集合须共同覆盖 must_cover / goals 全部支柱（不是每条只碰 ≥1 项就够）",
            "constraints（含对话推断的禁做/只用）外的 scope 扩展需 request_human_input",
            "上游 survey/KB 是证据，不是把用户全量诉求退化成 survey 窄 gap 的借口",
            "desirable_attributes 优先在 HIF 打 Q/I 时参考",
        ],
    }


async def _get_research_goal(state: State, **_: Any) -> dict[str, Any]:
    inputs = state.hook_state.get("node_inputs") or {}
    if not isinstance(inputs, dict):
        inputs = {}

    dialogue = build_dialogue_context(state, inputs)
    parsed = parse_research_goal(inputs, dialogue=dialogue)
    state.hook_state["research_goal_parsed"] = parsed
    state.hook_state["dialogue_context"] = dialogue

    goal = parsed["goal"]
    lines = ["Research goal parsed:"]
    if goal.get("title"):
        lines.append(f"  title: {goal['title'][:100]}")
    for field in ("goals", "preferences", "constraints", "desirable_attributes"):
        val = goal.get(field)
        if val:
            if isinstance(val, list):
                lines.append(f"  {field}: {len(val)} item(s)")
            else:
                lines.append(f"  {field}: {str(val)[:80]}")

    themes = parsed.get("must_cover_themes") or []
    if themes:
        lines.append(f"  must_cover_themes ({len(themes)}): {', '.join(themes)}")
        lines.append("  anti_collapse: 覆盖不足 → audit/validate 将失败，禁止只做其中一小点")

    locked = parsed.get("locked_definitions") or []
    if locked:
        labels = [e.get("label", "?") for e in locked if isinstance(e, dict)]
        lines.append(f"  locked_definitions ({len(locked)}): {', '.join(str(x) for x in labels)}")
        lines.append("  anti_rewrite: 改类别名/定义 → audit_definition_fidelity / validate 将失败")

    it = parsed["iteration"]
    # 只在调用方**真的指定了**预算时才报数字。没指定就明说由课题决定 ——
    # 框架报一个数，那个数就会变成锚（实测：默认 3 → 模型正好交 3 个）。
    budget = [f"max_rounds={it['max_refine_rounds']}"]
    if it.get("target_prereg") is not None:
        budget.insert(0, f"调用方指定的问题数={it['target_prereg']}")
    if it.get("min_candidates") is not None:
        budget.insert(0, f"调用方指定的最少候选数={it['min_candidates']}")
    lines.append("  iteration: " + ", ".join(budget))
    if it.get("target_prereg") is None:
        lines.append(
            "  问题数：**由课题决定** —— 用户诉求拆出来是几个就是几个（1 个也行，"
            "5 个也行）。框架不给目标数，别凑数，也别为了显得全面硬拆。"
        )
    lines.append(format_dialogue_brief(dialogue))

    return {
        "status": "success",
        "message": "\n".join(lines),
        **parsed,
    }


register_tool(
    ToolDefinition(
        name="get_research_goal",
        description=(
            "解析 research goal，并加载**用户 prompt + 父会话对话记录**做 grounding。\n\n"
            "**Use when**（workflow 第 0 步，读 survey 之前必调）：\n"
            "  - 确认 goals / preferences / constraints / desirable_attributes\n"
            "  - 拿到最新 user_prompt、近期对话、从对话推断的禁做/只用约束\n"
            "  - 获取 hypothesis_iteration 预算\n\n"
            "假设与 research_plan **必须**对齐返回的 user_prompt / must_cover_themes / "
            "locked_definitions；\n"
            "多支柱诉求禁止退化；用户分类定义禁止改写；上游 artifact 只是证据。\n"
            "兼容 legacy：仅有 research_question + scope_hint 时自动映射。"
        ),
        parameters_schema={"type": "object", "properties": {}},
        allowed_node_types=["hypothesis"],
    ),
    _get_research_goal,
)
