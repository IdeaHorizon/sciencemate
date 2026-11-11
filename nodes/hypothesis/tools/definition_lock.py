"""Request-scoped taxonomy constraints come from explicit typed inputs.

Free-text extraction is a candidate view, never an authority or a freeze request.
Derived goal/dialogue/audit caches cannot establish user intent.
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from .artifact_save import save_hypothesis_singleton

_DEF_INPUT_KEYS = (
    "locked_taxonomy",
    "operation_categories",
)

_HEADING = re.compile(
    r"(?:^|\n)\s{0,3}#{0,3}\s*(?:分类定义|操作类别|类别定义|taxonomy|classification|"
    r"category\s*definitions?|操作分类|类别表)\s*[:：]?\s*\n",
    flags=re.IGNORECASE,
)
_LABEL_DEF_LINE = re.compile(
    r"(?:^|[\n])\s*(?:[-*]|\d+[\.\)、]|[（(]?\d+[)）])?\s*"
    r"[「\"'【\[]?(?P<label>[^：:\n\|\]]{2,40})[」\"'】\]]?\s*[:：|]\s*"
    r"(?P<defn>[^\n]{4,240})",
)
_DEFINED_AS = re.compile(
    r"[「\"']?(?P<label>[^「」\"'\n]{2,40})[」\"']?\s*"
    r"(?:定义为|定义是|是指|指的是|means?|defined\s+as)\s*"
    r"[「\"']?(?P<defn>[^。；;\n]{4,200})",
    flags=re.IGNORECASE,
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _clean_label(text: str) -> str | None:
    t = re.sub(r"\s+", " ", (text or "").strip(" \t\n\r，,。.;；:：、|-"))
    t = re.sub(r"^(?:类别|分类|操作|category|class|type)\s*", "", t, flags=re.IGNORECASE)
    if len(t) < 2 or len(t) > 40:
        return None
    if t.lower() in {
        "定义", "说明", "备注", "name", "label", "definition", "类别", "分类",
    }:
        return None
    return t


def _clean_defn(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip(" \t\n\r，,。.;；"))


def _entry(label: str, definition: str = "", *, source: str = "parsed") -> dict[str, str]:
    return {
        "label": label,
        "definition": _clean_defn(definition),
        "source": source,
    }


def _dedupe_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for e in entries:
        label = e.get("label") or ""
        key = label.lower()
        if not key:
            continue
        if key in seen:
            for i, prev in enumerate(out):
                if prev["label"].lower() == key and len(e.get("definition") or "") > len(
                    prev.get("definition") or ""
                ):
                    out[i] = e
            continue
        seen.add(key)
        out.append(e)
    return out


def _coerce_entry(item: Any, *, source: str = "explicit", literal: bool = False) -> dict[str, str] | None:
    def label_text(value: str) -> str | None:
        return (value.strip() or None) if literal else _clean_label(value)

    def entry(label: str, definition: str) -> dict[str, str]:
        if literal:
            return {"label": label, "definition": definition.strip(), "source": source, "scope": "request"}
        return _entry(label, definition, source=source)

    if isinstance(item, str):
        if ":" in item or "：" in item:
            parts = re.split(r"[:：]", item, maxsplit=1)
            label = label_text(parts[0])
            if label:
                return entry(label, parts[1] if len(parts) > 1 else "")
        label = label_text(item)
        return entry(label, "") if label else None
    if isinstance(item, dict):
        label = None
        for key in ("label", "name", "category", "class", "id", "title", "操作", "类别"):
            val = item.get(key)
            if isinstance(val, str) and label_text(val):
                label = label_text(val)
                break
        if not label:
            return None
        defn = ""
        for key in ("definition", "defn", "desc", "description", "meaning", "定义", "说明"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                defn = val
                break
        return entry(label, defn)
    return None


def extract_locked_definitions_from_inputs(
    node_inputs: dict[str, Any], *, source_prefix: str = "node_inputs",
) -> list[dict[str, str]]:
    """Pull explicit taxonomy / classification locks from node_inputs."""
    if not isinstance(node_inputs, dict):
        return []
    entries: list[dict[str, str]] = []

    def _ingest(val: Any, source: str) -> None:
        if isinstance(val, list):
            for item in val:
                e = _coerce_entry(item, source=source, literal=True)
                if e:
                    entries.append(e)
        elif isinstance(val, dict):
            looks_like_map = all(isinstance(k, str) for k in val.keys()) and not any(
                k in val for k in ("label", "name", "category")
            )
            if looks_like_map and not any(
                isinstance(v, (list, dict)) for v in val.values()
            ):
                for k, v in val.items():
                    e = _coerce_entry(
                        {"label": k, "definition": str(v) if v is not None else ""},
                        source=source, literal=True,
                    )
                    if e:
                        entries.append(e)
            else:
                e = _coerce_entry(val, source=source, literal=True)
                if e:
                    entries.append(e)

    for key in _DEF_INPUT_KEYS:
        if key in node_inputs:
            _ingest(node_inputs.get(key), f"{source_prefix}.{key}")
    rg = node_inputs.get("research_goal")
    if isinstance(rg, dict):
        for key in _DEF_INPUT_KEYS:
            if key in rg:
                _ingest(rg.get(key), f"{source_prefix}.research_goal.{key}")
    return _dedupe_entries(entries)


def extract_definition_candidates_from_text(
    text: str,
    *,
    source: str = "user_prompt",
) -> list[dict[str, str]]:
    """Heuristic candidates only, including uncertain key/value or list matches."""
    if not text or not str(text).strip():
        return []
    body = str(text)
    entries: list[dict[str, str]] = []

    chunks: list[str] = []
    for m in _HEADING.finditer(body):
        start = m.end()
        nxt = _HEADING.search(body, start)
        rest = body[start : (nxt.start() if nxt else len(body))]
        stop = re.search(r"\n#{1,3}\s+\S", rest)
        chunks.append(rest[: stop.start()] if stop else rest[:1200])
    scan_targets = chunks if chunks else [body]

    for chunk in scan_targets:
        for m in _LABEL_DEF_LINE.finditer("\n" + chunk):
            label = _clean_label(m.group("label"))
            defn = _clean_defn(m.group("defn"))
            if label and defn:
                entries.append(_entry(label, defn, source=source))
        for m in _DEFINED_AS.finditer(chunk):
            label = _clean_label(m.group("label"))
            defn = _clean_defn(m.group("defn"))
            if label and defn:
                entries.append(_entry(label, defn, source=source))

    return _dedupe_entries(entries)[:24]


def build_definition_lock(
    node_inputs: dict[str, Any] | None,
    user_prompt: str | None = None,
    *, source_prefix: str = "node_inputs",
) -> dict[str, Any]:
    """Assemble locked taxonomy for grounding + fidelity audit."""
    inputs = node_inputs if isinstance(node_inputs, dict) else {}
    locked = extract_locked_definitions_from_inputs(inputs, source_prefix=source_prefix)
    labels = [e["label"] for e in locked]
    return {
        "locked_definitions": locked,
        "locked_labels": labels,
        "n_locked": len(locked),
        "has_definitions": any(e.get("definition") for e in locked),
        "definition_candidates": extract_definition_candidates_from_text(user_prompt or ""),
        "checklist": [
            "用户给定类别名与定义只读：禁止重命名 / 合并 / 拆分 / 用文献 taxonomy 替换",
            "仅在本次显式分类约束的作用域内核对定义；文本候选不是锁定定义",
            "claim / 实验对象只能使用 locked 类别；要改 ontology → request_human_input",
            "文献分类仅可作对照 baseline，不得替换用户定义",
        ] if locked else [],
    }


def _defn_tokens(definition: str) -> list[str]:
    if not definition:
        return []
    tokens: list[str] = []
    tokens.extend(w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", definition))
    for span in re.findall(r"[\u4e00-\u9fff]{2,}", definition):
        tokens.append(span)
        if len(span) > 4:
            tokens.extend(span[i : i + 2] for i in range(0, len(span) - 1, 2))
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:12]


def definition_present(definition: str, corpus_norm: str) -> bool:
    if not definition:
        return True
    d_norm = _norm(definition)
    if len(d_norm) >= 6 and d_norm[:40] in corpus_norm:
        return True
    clause = re.split(r"[；;。]", definition, maxsplit=1)[0]
    c_norm = _norm(clause)
    if 6 <= len(c_norm) <= 80 and c_norm in corpus_norm:
        return True
    tokens = _defn_tokens(definition)
    if not tokens:
        return True
    hits = sum(1 for t in tokens if _norm(t) in corpus_norm)
    need = 1 if len(tokens) <= 2 else max(2, (len(tokens) + 2) // 3)
    return hits >= need


def assess_definition_fidelity(
    locked: list[dict[str, str]],
    *,
    prereg: str,
    plan: str = "",
    overview: str = "",
) -> dict[str, Any]:
    """Fail when prereg/plan rewrite or drop user-locked category definitions."""
    locked = [e for e in locked if e.get("label")]
    prereg_norm = _norm(prereg)
    plan_norm = _norm(plan)
    overview_norm = _norm(overview)
    combined = prereg_norm + plan_norm + overview_norm

    if not locked:
        return {
            "applicable": False,
            "passed": True,
            "n_locked": 0,
            "missing_labels": [],
            "rewritten_definitions": [],
            "plan_missing_labels": [],
            "reason": "无 locked_definitions",
            "per_entry": [],
        }

    if len(locked) < 2 and not any(e.get("definition") for e in locked):
        return {
            "applicable": False,
            "passed": True,
            "n_locked": len(locked),
            "missing_labels": [],
            "rewritten_definitions": [],
            "plan_missing_labels": [],
            "reason": "无锁定分类定义（<2 类且无定义文本）：跳过保真门禁",
            "per_entry": [],
        }

    per_entry: list[dict[str, Any]] = []
    missing_labels: list[str] = []
    rewritten: list[str] = []

    for e in locked:
        label = e["label"]
        defn = e.get("definition") or ""
        label_in_prereg = bool(_norm(label)) and _norm(label) in prereg_norm
        label_in_plan = (not plan_norm) or (_norm(label) in plan_norm)
        defn_ok = definition_present(defn, prereg_norm) if defn else True
        renamed = (not label_in_prereg) and bool(defn) and definition_present(defn, prereg_norm)

        per_entry.append({
            "label": label,
            "label_in_prereg": label_in_prereg,
            "label_in_plan": label_in_plan,
            "definition_preserved": defn_ok,
            "likely_renamed": renamed,
        })
        if not label_in_prereg:
            missing_labels.append(label)
        if defn and not defn_ok and label_in_prereg:
            rewritten.append(label)
        if renamed:
            rewritten.append(f"{label}(疑似改名)")

    plan_missing: list[str] = []
    if plan_norm and len(locked) >= 2:
        for e in locked:
            if _norm(e["label"]) not in plan_norm:
                plan_missing.append(e["label"])

    passed = not missing_labels and not rewritten and not plan_missing
    parts: list[str] = []
    if missing_labels:
        parts.append(f"prereg 缺失用户类别: {', '.join(missing_labels)}")
    if rewritten:
        parts.append(f"定义被改写/疑似改名: {', '.join(rewritten)}")
    if plan_missing:
        parts.append(f"research_plan 未沿用类别: {', '.join(plan_missing)}")

    if passed:
        reason = f"全部 {len(locked)} 个锁定类别名与定义在 prereg 中保真"
    else:
        reason = (
            "定义保真失败（禁止改写用户分类）："
            + "；".join(parts)
            + "。须恢复用户原文类别/定义，或 request_human_input 确认改 ontology。"
        )

    return {
        "applicable": True,
        "passed": passed,
        "n_locked": len(locked),
        "missing_labels": missing_labels,
        "rewritten_definitions": rewritten,
        "plan_missing_labels": plan_missing,
        "reason": reason,
        "per_entry": per_entry,
        "unused_combined_hint": bool(combined),
    }


def resolve_definition_lock(
    state: State, node_inputs: dict | None = None, *, user_prompt: str = "",
) -> dict:
    """Rebuild from request inputs/receipt; never trust a derived lock cache."""
    from core.context_engine import read_first_turn_node_inputs_receipt

    inputs = node_inputs if node_inputs is not None else state.hook_state.get("node_inputs")
    inputs = inputs if isinstance(inputs, dict) else {}
    nested = inputs.get("research_goal")
    explicit = any(k in inputs or isinstance(nested, dict) and k in nested for k in _DEF_INPUT_KEYS)
    source = "node_inputs"
    note = None
    if not explicit:
        try:
            inputs = read_first_turn_node_inputs_receipt(state)["values"]
            source = "first_turn_node_inputs"
        except FileNotFoundError:
            pass  # Legacy runs may lack a receipt; caches are not a substitute.
        except (OSError, ValueError, KeyError) as exc:
            note = f"definition input receipt unavailable: {type(exc).__name__}"
    lock = build_definition_lock(inputs, user_prompt=user_prompt, source_prefix=source)
    if note:
        lock["source_note"] = note
    return lock


def resolve_locked_definitions(state: State) -> list[dict[str, str]]:
    return resolve_definition_lock(state)["locked_definitions"]


def _read_latest_content(state: State, artifact_type: str) -> str:
    arts = state.list_artifacts(artifact_type)
    if not arts:
        return ""
    rec = state.read_artifact(arts[-1]["id"]) or {}
    return str(rec.get("content") or "")


async def _audit_definition_fidelity(
    state: State,
    locked_definitions: list[dict[str, Any]] | None = None,
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    locked = resolve_locked_definitions(state)

    prereg = _read_latest_content(state, "pre_registration")
    plan = _read_latest_content(state, "research_plan")
    overview = _read_latest_content(state, "hypothesis_research_overview")

    if locked and not prereg and not plan:
        return {
            "status": "error",
            "passed": False,
            "error": "缺少 pre_registration / research_plan，无法做定义保真审计",
            "locked_definitions": locked,
        }

    report = assess_definition_fidelity(
        locked, prereg=prereg, plan=plan, overview=overview,
    )

    artifact_id: str | None = None
    if save_report:
        body = {"locked_definitions": locked, **report}
        lines = [
            "# Definition Fidelity Audit",
            "",
            f"- **passed**: {report['passed']}",
            f"- applicable: {report['applicable']}",
            f"- n_locked: {report['n_locked']}",
            "",
            report["reason"],
            "",
            "## Locked taxonomy",
        ]
        for e in locked:
            defn = e.get("definition") or "(label only)"
            lines.append(f"- **{e['label']}**: {defn[:160]}")
        lines.append("")
        lines.append("## Per entry")
        for row in report.get("per_entry") or []:
            marks = [
                "prereg✓" if row.get("label_in_prereg") else "prereg✗",
                "defn✓" if row.get("definition_preserved") else "defn✗",
            ]
            if row.get("likely_renamed"):
                marks.append("renamed?")
            lines.append(f"- **{row['label']}**: {', '.join(marks)}")
        content = (
            "\n".join(lines)
            + "\n\n```json\n"
            + json.dumps(body, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
        saved = save_hypothesis_singleton(
            state,
            "hypothesis_definition_fidelity",
            "Definition_Fidelity",
            content,
            metadata={
                "passed": report["passed"],
                "n_locked": report["n_locked"],
                "missing_labels": report.get("missing_labels") or [],
            },
        )
        artifact_id = saved["id"]

    state.hook_state["last_definition_fidelity"] = {
        "locked_definitions": locked,
        **report,
    }

    return {
        "status": "success",
        "passed": report["passed"],
        "locked_definitions": locked,
        "artifact_id": artifact_id,
        "candidate_definitions": locked_definitions or [],
        "message": report["reason"],
        **report,
    }


register_tool(
    ToolDefinition(
        name="audit_definition_fidelity",
        description=(
            "按请求的结构化 locked_taxonomy / operation_categories（source + request scope）审计分类定义保真；"
            "不从自由文本、派生缓存或本工具参数建立强制约束；无显式分类约束时不适用。"
            "防止 prereg 重命名、合并或用文献 taxonomy 替换用户 ontology。\n\n"
            "**Use when**：\n"
            "  - save pre_registration（草稿）/ research_plan 之后\n"
            "  - **freeze_artifact 之前**（有 locked_definitions 时必调）\n"
            "  - validate_hypothesis_outputs 之前\n\n"
            "passed=false → 恢复用户原文类别与定义并重 save 未冻结 prereg；"
            "禁止带着改写分类 freeze / 结束。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "locked_definitions": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "可选候选 [{label, definition}]；仅作线索回显，不改变显式请求约束或 passed 判据",
                },
                "save_report": {"type": "boolean", "default": True},
            },
        },
        allowed_node_types=["hypothesis"],
    ),
    _audit_definition_fidelity,
)
