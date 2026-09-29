"""Load user prompt + parent chat dialogue for hypothesis grounding.

chat.py 把对话持久化到 orchestrator__<project>/conversation.json；
hypothesis 作为 run_node 子节点时，survey/artifact 之外还必须对齐：
  1) 本次启动时的用户诉求（node_inputs / 最近一条 user 消息）
  2) 近期对话里明确的约束、禁做项、范围修正

不调用 conversation_store.load_conversation（会写回 parent state 元数据）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.state import State

from .definition_lock import build_definition_lock

_USER_PROMPT_KEYS = (
    "user_prompt",
    "user_request",
    "prompt",
    "task",
    "instruction",
    "research_question",
    "query",
)
_MUST_COVER_KEYS = (
    "must_cover",
    "must_cover_themes",
    "must_cover_pillars",
    "required_themes",
)
_MAX_TURNS = 12
_MAX_CHARS_PER_MSG = 800
_MAX_TOTAL_CHARS = 6000
# user_prompt / must_cover 是防退化锚点，brief 里尽量完整保留
_MAX_USER_PROMPT_BRIEF = 2400

_INCLUDE_LEAD = re.compile(
    r"(?:包含|涵盖|包括|涉及|含有|覆盖|需(?:要|求)?(?:覆盖|包含|研究)|"
    r"focus(?:es|ing)?\s+on|including|covers?|comprising)\s*[:：]?\s*",
    flags=re.IGNORECASE,
)
# `/` 只在两侧有空白时才是并列分隔（"A / B"）；紧贴的 `ρ_p/ρ_f=1.5-10` 是一个
# 比值表达式，拆开就成了 `ρ_p` + `ρ_f=1.5-10`，目标覆盖审计据此持续误报
# "ρ_f=1.5-10 未覆盖"（#262，E2E 流体力学实拍）。
_SPLIT_THEME = re.compile(r"[、，,;｜|]|\s+/\s+|以及|及|和|与|and\s+", flags=re.IGNORECASE)
_NUMBERED_ITEM = re.compile(
    r"(?:^|[\n;；])\s*(?:\d+[\.\)、]|[（(]?\d+[)）]|[a-zA-Z][\.\)])\s*([^\n;；]{2,80})",
)


def _orchestrator_conversation_path(state: State) -> Path | None:
    """Locate parent orchestrator conversation.json (sibling of this run)."""
    parent = state.root.parent
    candidates: list[Path] = []
    if state.parent_run_id:
        candidates.append(parent / state.parent_run_id / "conversation.json")
    if state.project_id:
        candidates.append(
            parent / f"orchestrator__{state.project_id}" / "conversation.json"
        )
    # de-dupe while preserving order
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            return path
    return None


def _read_conversation_messages(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        msgs = data.get("messages") or []
        return [m for m in msgs if isinstance(m, dict)]
    return []


def _clip(text: str, max_len: int = _MAX_CHARS_PER_MSG) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _is_dialogue_role(role: str) -> bool:
    return role in {"user", "assistant"}


def extract_user_prompt_from_inputs(node_inputs: dict[str, Any]) -> str | None:
    """Best-effort user prompt from node_inputs (orchestrator 传入字段)。"""
    if not isinstance(node_inputs, dict):
        return None
    for key in _USER_PROMPT_KEYS:
        val = node_inputs.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, list) and val:
            parts = [str(x).strip() for x in val if str(x).strip()]
            if parts:
                return "\n".join(parts)
    rg = node_inputs.get("research_goal")
    if isinstance(rg, dict):
        goals = rg.get("goals")
        if isinstance(goals, list) and goals:
            return "\n".join(str(g).strip() for g in goals if str(g).strip())
        if isinstance(rg.get("title"), str) and rg["title"].strip():
            return rg["title"].strip()
    return None


def extract_constraints_from_text(text: str) -> list[str]:
    """Heuristic: pull explicit forbid/only-use lines from user text."""
    if not text:
        return []
    found: list[str] = []
    patterns = (
        r"(?:禁止|勿|不要|不得|严禁)[^。；;\n]{2,120}",
        r"(?:只(?:能|准|允许)?使用|仅使用|只用)[^。；;\n]{2,120}",
        r"(?:必须|务必)[^。；;\n]{2,120}",
        r"(?:forbidden|must not|do not|only use)[^.\n]{2,120}",
    )
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            snippet = m.group(0).strip(" ，,。.;；")
            if snippet and snippet not in found:
                found.append(snippet)
    return found[:12]


def _clean_theme(text: str) -> str | None:
    t = re.sub(r"\s+", " ", (text or "").strip(" \t\n\r，,。.;；:：、-/"))
    t = re.sub(r"^(?:以及|及|和|与|and)\s+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:等|等等|etc\.?)$", "", t, flags=re.IGNORECASE).strip()
    if len(t) < 2 or len(t) > 60:
        return None
    # Drop soft filler that isn't a research pillar
    if t.lower() in {"研究", "调研", "分析", "设计", "完整", "全面", "the", "a", "an"}:
        return None
    return t


def _dedupe_themes(themes: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in themes:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out[:12]


def extract_must_cover_themes(
    text: str,
    *,
    explicit: list[Any] | None = None,
) -> list[str]:
    """Extract multi-pillar themes the run must not collapse away from.

    Prefer explicit lists from node_inputs; else parse「包含 A、B、C和D」/
    numbered items. Single vague topic → empty list (no hard multi-cover gate).
    """
    themes: list[str] = []
    if explicit:
        for item in explicit:
            if isinstance(item, str):
                cleaned = _clean_theme(item)
                if cleaned:
                    themes.append(cleaned)
            elif isinstance(item, dict):
                for key in ("theme", "name", "title", "pillar", "text"):
                    val = item.get(key)
                    if isinstance(val, str) and _clean_theme(val):
                        themes.append(_clean_theme(val) or "")
                        break

    body = (text or "").strip()
    if body:
        for m in _NUMBERED_ITEM.finditer("\n" + body):
            cleaned = _clean_theme(m.group(1))
            if cleaned:
                themes.append(cleaned)

        for m in _INCLUDE_LEAD.finditer(body):
            tail = body[m.end():]
            # Stop at sentence boundary if present
            stop = re.search(r"[。；;\n]", tail)
            chunk = tail[: stop.start()] if stop else tail
            # Prefer the first clause of parallel themes
            chunk = re.split(r"[。！？!?]", chunk, maxsplit=1)[0]
            parts = _SPLIT_THEME.split(chunk)
            for part in parts:
                cleaned = _clean_theme(part)
                if cleaned:
                    themes.append(cleaned)

    return _dedupe_themes([t for t in themes if t])


def extract_must_cover_from_inputs(node_inputs: dict[str, Any]) -> list[str]:
    """Pull explicit must-cover lists from node_inputs / research_goal."""
    if not isinstance(node_inputs, dict):
        return []
    explicit: list[Any] = []
    for key in _MUST_COVER_KEYS:
        val = node_inputs.get(key)
        if isinstance(val, list):
            explicit.extend(val)
        elif isinstance(val, str) and val.strip():
            explicit.extend(_SPLIT_THEME.split(val))
    rg = node_inputs.get("research_goal")
    if isinstance(rg, dict):
        for key in _MUST_COVER_KEYS:
            val = rg.get(key)
            if isinstance(val, list):
                explicit.extend(val)
            elif isinstance(val, str) and val.strip():
                explicit.extend(_SPLIT_THEME.split(val))
        goals = rg.get("goals")
        # Multiple short goal strings → treat as pillars (not one long paragraph)
        if isinstance(goals, list) and len(goals) >= 2:
            short = [g for g in goals if isinstance(g, str) and 2 <= len(g.strip()) <= 40]
            if len(short) >= 2:
                explicit.extend(short)
    return extract_must_cover_themes("", explicit=explicit)


def build_dialogue_context(
    state: State,
    node_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble user_prompt + recent dialogue brief for hypothesis grounding."""
    inputs = node_inputs if isinstance(node_inputs, dict) else {}
    path = _orchestrator_conversation_path(state)
    messages = _read_conversation_messages(path) if path else []

    dialogue_turns: list[dict[str, str]] = []
    total = 0
    # Walk newest-first so the latest user prompt is never dropped by the budget.
    for msg in reversed(messages):
        role = str(msg.get("role") or "")
        if not _is_dialogue_role(role):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        clipped = _clip(content)
        cost = len(clipped)
        if total + cost > _MAX_TOTAL_CHARS and dialogue_turns:
            break
        dialogue_turns.append({"role": role, "content": clipped})
        total += cost
        if len(dialogue_turns) >= _MAX_TURNS:
            break
    dialogue_turns.reverse()

    latest_user_from_chat = next(
        (t["content"] for t in reversed(dialogue_turns) if t["role"] == "user"),
        None,
    )
    from_inputs = extract_user_prompt_from_inputs(inputs)
    # Prefer full node_inputs prompt (not clipped chat turn) when available
    user_prompt = from_inputs or latest_user_from_chat
    if from_inputs is None and path and messages:
        # Re-read latest user message without dialogue budget clip
        for msg in reversed(messages):
            if str(msg.get("role") or "") == "user":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    user_prompt = content.strip()
                    break

    constraint_pool = "\n".join(
        filter(None, [
            user_prompt or "",
            *(t["content"] for t in dialogue_turns if t["role"] == "user"),
        ])
    )
    inferred_constraints = extract_constraints_from_text(constraint_pool)

    from_inputs_themes = extract_must_cover_from_inputs(inputs)
    parsed_themes = extract_must_cover_themes(user_prompt or "")
    must_cover = _dedupe_themes([*from_inputs_themes, *parsed_themes])

    def_lock = build_definition_lock(inputs, user_prompt=user_prompt)
    locked_definitions = def_lock["locked_definitions"]
    locked_labels = def_lock["locked_labels"]

    recent_users = [t["content"] for t in dialogue_turns if t["role"] == "user"]

    grounding = [
        # Completion gate first — extra audits must not burn the turn budget.
        "【完成闸优先】先闭合 required outputs（research_state / pre_registration / "
        "research_plan / hypothesis_innovation_report / hypothesis_research_overview）"
        "与 validate_hypothesis_outputs(passed=true)；额外 audit/cluster/evolve 不得挤占轮次",
        "【完成闸优先】有 prereg 或 HIF 后尽早 save research_plan + overview + "
        "research_state，并立刻跑 validate；失败项当场修，勿先堆更多 audit artifact",
        "禁止把多支柱用户诉求退化成其中单一小点；must_cover_themes 须全部覆盖",
        "最终 hypothesis 集合 + research_plan 合集必须覆盖全部 must_cover_themes",
        "research_plan / computational_workflow 必须遵守 inferred_constraints 与对话中的禁做/只用约束",
        "上游 survey/KB 只作证据与 gap 来源；不得用 survey 窄 gap 替换用户全量诉求",
        "若 user_prompt 为空，先 request_human_input 澄清，勿仅凭 artifact 臆造方向",
    ]
    grounding.extend(def_lock.get("checklist") or [])

    return {
        "user_prompt": user_prompt,
        "user_prompt_source": (
            "node_inputs" if from_inputs
            else ("conversation" if latest_user_from_chat else None)
        ),
        "must_cover_themes": must_cover,
        "locked_definitions": locked_definitions,
        "locked_labels": locked_labels,
        "recent_user_turns": recent_users[-5:],
        "recent_dialogue": dialogue_turns,
        "inferred_constraints": inferred_constraints,
        "conversation_path": str(path) if path else None,
        "n_conversation_messages": len(messages),
        "grounding_checklist": grounding,
    }


def format_dialogue_brief(ctx: dict[str, Any]) -> str:
    lines = ["Dialogue / user-prompt grounding:"]
    up = ctx.get("user_prompt")
    if up:
        src = ctx.get("user_prompt_source") or "?"
        lines.append(f"  user_prompt ({src}): {_clip(str(up), _MAX_USER_PROMPT_BRIEF)}")
    else:
        lines.append("  user_prompt: (missing)")
    # Surface completion-gate priority first so get_research_goal brief leads with it.
    gate_items = [
        c for c in (ctx.get("grounding_checklist") or [])
        if isinstance(c, str) and c.startswith("【完成闸优先】")
    ]
    if gate_items:
        lines.append("  completion_gate (最高优先 — 先于额外 audit):")
        for c in gate_items:
            lines.append(f"    - {_clip(c, 220)}")
    themes = ctx.get("must_cover_themes") or []
    if themes:
        lines.append(f"  must_cover_themes ({len(themes)}) — 禁止退化遗漏:")
        for t in themes:
            lines.append(f"    - {t}")
    locked = ctx.get("locked_definitions") or []
    if locked:
        lines.append(f"  locked_definitions ({len(locked)}) — 禁止改写类别/定义:")
        for e in locked[:12]:
            defn = e.get("definition") or ""
            if defn:
                lines.append(f"    - {e.get('label')}: {_clip(defn, 120)}")
            else:
                lines.append(f"    - {e.get('label')}")
    cons = ctx.get("inferred_constraints") or []
    if cons:
        lines.append(f"  inferred_constraints ({len(cons)}):")
        for c in cons:
            lines.append(f"    - {_clip(c, 160)}")
    turns = ctx.get("recent_dialogue") or []
    if turns:
        lines.append(f"  recent_dialogue ({len(turns)} turns):")
        for t in turns[-6:]:
            lines.append(f"    [{t['role']}] {_clip(t['content'], 200)}")
    path = ctx.get("conversation_path")
    if path:
        lines.append(f"  conversation_path: {path}")
    return "\n".join(lines)
