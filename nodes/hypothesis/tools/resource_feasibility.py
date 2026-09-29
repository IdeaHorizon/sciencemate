"""audit_resource_feasibility — 高成本资源承诺必须可确认或可降级。

预注册若要求多模型、多人标注、大规模人工审计、多个完整 benchmark 等，
须在 research_plan/prereg 写明 ## resource_feasibility：
每条承诺 status ∈ confirmed | assumed | deferred，
assumed 必须有降级方案，deferred 不得充当核心证伪依赖。
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State

from ..committed import committed_claim_texts
from core.tool_registry import ToolDefinition, register_tool

from .artifact_save import save_hypothesis_singleton

_STATUSES = frozenset({"confirmed", "assumed", "deferred"})

_KIND_SPECS: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    (
        "multi_model",
        re.compile(
            r"(?:多个|多款|至少\s*\d+\s*个?|≥\s*\d+|>=\s*\d+|"
            r"[2-9]\s*个|[\d二三四五六七八九十]+\s*个)\s*"
            r"(?:模型|大模型|LLM|基座模型)|"
            r"(?:evaluate|eval|compare|对比|比较)\s+"
            r"(?:across\s+)?(?:\d+|multiple|several)\s+models?|"
            r"(?:\d+|multiple|several)\s+models?\s*(?:×|x|and|,)|"
            r"(?:GPT|Claude|Gemini|LLaMA|Llama|Qwen|DeepSeek).{0,40}"
            r"(?:GPT|Claude|Gemini|LLaMA|Llama|Qwen|DeepSeek)|"
            r"多名?\s*模型",
            flags=re.IGNORECASE,
        ),
        ("multi_model", "multiple_models", "多模型", "多个模型", "llm_panel"),
    ),
    (
        "human_annotators",
        re.compile(
            r"(?:多名|多个|至少\s*\d+|≥\s*\d+|[2-9]\s*名?|"
            r"[\d二三四五六七八九十]+\s*名)\s*"
            r"(?:人工)?(?:标注者|标注员|annotator)|"
            r"(?:人工标注|human\s+annotat|crowd\s*sourc|"
            r"标注者\s*[×x]\s*\d+|inter[\s-]?annotator)",
            flags=re.IGNORECASE,
        ),
        ("human_annotators", "annotators", "人工标注", "标注者", "annotator"),
    ),
    (
        "human_audit",
        re.compile(
            r"(?:大规模|全面|全量|大量)\s*(?:人工\s*)?(?:审计|审核|质检)|"
            r"(?:人工\s*)?(?:审计|审核)\s*(?:全部|全量|大规模)|"
            r"human\s+(?:audit|review)\s*(?:of\s+)?(?:all|full|large)|"
            r"large[\s-]?scale\s+human\s+(?:audit|review)",
            flags=re.IGNORECASE,
        ),
        ("human_audit", "manual_audit", "人工审计", "大规模人工"),
    ),
    (
        "full_benchmarks",
        re.compile(
            r"(?:[2-9]|[\d二三四五六七八九十]+)\s*个\s*(?:完整\s*)?"
            r"(?:benchmark|基准(?:测试)?|评测集)|"
            r"(?:三个|多个)\s*完整\s*(?:benchmark|基准)|"
            r"(?:full|complete|entire)\s+(?:suite\s+of\s+)?"
            r"(?:benchmarks?|eval\s*suites?)|"
            r"(?:\d+)\s+full\s+benchmarks?",
            flags=re.IGNORECASE,
        ),
        (
            "full_benchmarks",
            "complete_benchmarks",
            "完整benchmark",
            "完整基准",
            "三个完整",
        ),
    ),
)

_SECTION_ALIASES = (
    "resource_feasibility",
    "resource feasibility",
    "feasibility",
    "资源可行性",
    "可执行性",
    "资源确认",
)

_COMMITMENT_FIELD_ALIASES = (
    "commitments",
    "commitment_inventory",
    "resource_commitments",
    "承诺清单",
    "资源承诺",
)

_FALLBACK_HINT = re.compile(
    r"(?:降级|fallback|回退|子集|subset|pilot|先导|抽检|抽样|"
    r"单模型|一个模型|primary\s+model|exploratory|可复现|"
    r"公开结果|deferred|延后)",
    flags=re.IGNORECASE,
)

_CONFIRMED_SOURCE_HINT = re.compile(
    r"(?:用户|对话|user|prompt|owner_config|config|配额|预算|"
    r"budget|已确认|confirmed|explicit|明确给)",
    flags=re.IGNORECASE,
)

_DEFERRED_CORE_HINT = re.compile(
    r"(?:不(?:进入|作为|充当)?核心|非核心|not\s+(?:in\s+)?core|"
    r"exploratory|后续工作|不进\s*falsifier|不依赖)",
    flags=re.IGNORECASE,
)


def _strip_feasibility_sections(text: str) -> str:
    if not text:
        return ""
    return re.sub(
        r"(?:^|\n)#{1,2}\s*(?:resource[_\s-]?feasibility|资源可行性|可执行性|资源确认)\s*\n"
        r".*?(?=\n#{1,2}\s+\S|\Z)",
        "\n",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )


# 否认信号：某个资源被明确声明为「不用/零/无」。
# ⚠️ 不含 `deferred`——「5 名 deferred（后续工作）」是**真承诺**（现有测试
#    test_detects/test_passes 都要求它被检测到），deferred 只是推迟不是不用。
_DISAVOWAL_RE = re.compile(
    r"(?<![1-9])\b0\b|(?<!\d)0\s*(?:名|个|人|次|套|models?)|数量为\s*0|"
    r"零(?:名|个|人)?|无任何|\b无\b|不使用|未使用|不需要|无需|"
    r"\bnone\b|no\s+(?:human|annotat|model|benchmark|audit)|not\s+used|"
    r"not\s+applicable|\bn/?a\b",
    flags=re.IGNORECASE,
)
# 正数量信号：即便同一行出现否认词，只要有 ≥1 的**资源**量就仍算承诺（防「不进入
# 核心但用 5 名」这类）。中文数字、命名模型、大规模/全量/完整都算正量。
#
# ⚠️ 2026-08-23 E2E v32 误报根因（PR#639 否定感知的漏）：第一分支的资源单位曾是
# **可选**（`(?:名|个|…)?`），于是**裸数字**也算正量 —— 一行「| 合计 | 单机CPU |
# < 3 分钟墙钟 | 无需人工标注 |」里，时长「3 分钟」的「3」被当成正资源量，否决了
# 「无需人工标注」的否认，把 human_annotators 误判成承诺。完成门于是永远不闭合，
# 模型循环补 resource_feasibility 段直到烧光 turn（单 run 1.5M tokens）。
# 修法：**资源单位必填**（去掉 `?`）——「5 名」仍算（有「名」），「3 分钟」不算
# （「分钟」不是资源单位）。正资源量必须自报单位，光一个数字（时长/编号/任何东西）
# 不足以否决一条明确的「无需 X」。
_POSITIVE_QTY_RE = re.compile(
    r"[1-9]\d*\s*(?:名|个|人|次|套|models?|annotators?|benchmarks?)|"
    r"[一二三四五六七八九十两]+\s*(?:名|个|人|次|套|完整)|"
    r"GPT-?\d|Claude|Gemini|多个|多模型|大规模|全量|完整套?|三套|三个",
    flags=re.IGNORECASE,
)


def _line_disavows(line: str) -> bool:
    """这一行是不是在**否认**某资源（零/无/不用），而非承诺它。

    判据 = 有否认信号 且 没有正数量信号。2026-08-23 E2E v29 实测误报：模型很规范
    地在 research_plan 资源表里写 `| human_annotators | 0 | deferred | 无任何人工
    标注 |`（明确声明不用），旧检测只看到 "annotator" 就判成「承诺了人工标注」。
    典型否定盲区：把「我不用 X」读成「我要用 X」。
    """
    return bool(_DISAVOWAL_RE.search(line)) and not _POSITIVE_QTY_RE.search(line)


def detect_resource_commitments(*texts: str) -> list[str]:
    """Return kinds of heavy commitments found outside feasibility section.

    否定感知：一个 kind 只在**有一行非否认地**提到它时才算承诺。所有提到它的行
    都是「0/无/不用」式否认（如资源表里 count=0 的行）→ 不算承诺（见 _line_disavows）。
    """
    blob = "\n".join(_strip_feasibility_sections(t or "") for t in texts)
    if not blob.strip():
        return []
    lines = blob.splitlines()
    found: list[str] = []
    for kind, pattern, _aliases in _KIND_SPECS:
        matching = [ln for ln in lines if pattern.search(ln)]
        if matching and any(not _line_disavows(ln) for ln in matching):
            found.append(kind)
    return found


def looks_like_heavy_resource_commitment(*texts: str) -> bool:
    return bool(detect_resource_commitments(*texts))


def extract_resource_feasibility(content: str) -> dict[str, str]:
    """Parse ## resource_feasibility and its ### subfields."""
    if not content:
        return {}
    section = ""
    for alias in _SECTION_ALIASES:
        m = re.search(
            r"(?:^|\n)#{1,2}\s*"
            + re.escape(alias)
            + r"\s*\n(.*?)(?=\n#{1,2}\s+\S|\Z)",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            section = m.group(1)
            break
    if not section.strip():
        return {}

    fields: dict[str, str] = {"_raw": section.strip()}
    # Collect ### headings
    parts = re.split(r"(?:^|\n)###\s+", section)
    if len(parts) > 1:
        for block in parts[1:]:
            lines = block.strip().splitlines()
            if not lines:
                continue
            key = lines[0].strip().lower()
            body = "\n".join(lines[1:]).strip()
            canon = _canonicalize_field(key)
            if canon:
                fields[canon] = body
            else:
                fields[key] = body
    else:
        # Flat section without ### — treat whole as commitments
        fields["commitments"] = section.strip()
    return fields


def _canonicalize_field(key: str) -> str | None:
    k = key.strip().lower().replace("-", "_").replace(" ", "_")
    for alias in _COMMITMENT_FIELD_ALIASES:
        if alias.replace(" ", "_").lower() == k or alias.lower() in key.lower():
            return "commitments"
    if k in ("confirmed_sources", "来源", "source", "sources"):
        return "confirmed_sources"
    if k in ("impact_on_core", "core_dependency", "核心依赖", "对核心的影响"):
        return "impact_on_core"
    if k in ("status_and_fallback", "降级方案", "fallback"):
        return "status_and_fallback"
    return None


def _parse_commitment_rows(body: str) -> list[dict[str, str]]:
    """Parse table rows or bullet lines into commitment dicts."""
    rows: list[dict[str, str]] = []
    if not body:
        return rows

    for line in body.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("|") and re.match(r"^\|?\s*:?-{2,}", raw):
            continue
        if raw.startswith("|") and re.search(r"kind|quantity|status|类型|数量", raw, re.I):
            # header
            continue

        kind = ""
        quantity = ""
        status = ""
        note = ""

        if raw.startswith("|"):
            cells = [c.strip() for c in raw.strip("|").split("|")]
            if len(cells) >= 3:
                kind, quantity, status = cells[0], cells[1], cells[2]
                note = cells[3] if len(cells) > 3 else ""
        else:
            # bullets: - multi_model: 3 LLMs; status=assumed; fallback=...
            m = re.match(r"^[-*]\s*(.+)$", raw)
            text = m.group(1) if m else raw
            # kind at start
            km = re.match(
                r"^(?P<kind>[A-Za-z_\u4e00-\u9fff]+)\s*[:：]\s*(?P<rest>.+)$",
                text,
            )
            if km:
                kind = km.group("kind")
                rest = km.group("rest")
            else:
                rest = text
                for k, _pat, aliases in _KIND_SPECS:
                    if any(a.lower() in text.lower() for a in aliases):
                        kind = k
                        break
            sm = re.search(
                r"status\s*[=:：]\s*(confirmed|assumed|deferred)",
                rest,
                flags=re.IGNORECASE,
            )
            if sm:
                status = sm.group(1).lower()
            else:
                for s in _STATUSES:
                    if re.search(rf"\b{s}\b", rest, flags=re.IGNORECASE):
                        status = s
                        break
            fm = re.search(
                r"(?:fallback|降级|source|来源)\s*[=:：]\s*(.+)$",
                rest,
                flags=re.IGNORECASE,
            )
            note = fm.group(1).strip() if fm else rest
            quantity = rest

        if not kind and not status:
            continue
        status_n = status.strip().lower()
        rows.append({
            "kind": _normalize_kind(kind) or kind.strip().lower(),
            "quantity": quantity.strip(),
            "status": status_n,
            "note": note.strip(),
            "raw": raw,
        })
    return rows


def _normalize_kind(kind: str) -> str | None:
    k = (kind or "").strip().lower().replace(" ", "_").replace("-", "_")
    for canon, _pat, aliases in _KIND_SPECS:
        if k == canon:
            return canon
        for a in aliases:
            if a.lower().replace(" ", "_") == k or a.lower() in kind.lower():
                return canon
    return None


def _row_covers_kind(row: dict[str, str], kind: str) -> bool:
    rk = row.get("kind") or ""
    if rk == kind:
        return True
    raw = (row.get("raw") or "") + " " + (row.get("quantity") or "")
    for canon, _pat, aliases in _KIND_SPECS:
        if canon != kind:
            continue
        if any(a.lower() in raw.lower() for a in aliases):
            return True
    return False


def assess_commitment_row(row: dict[str, str]) -> dict[str, Any]:
    issues: list[str] = []
    status = (row.get("status") or "").strip().lower()
    note = row.get("note") or row.get("quantity") or ""
    label = row.get("kind") or "?"

    if status not in _STATUSES:
        issues.append(
            f"status 无效/缺失（需为 {sorted(_STATUSES)} 之一）"
        )
    elif status == "confirmed":
        if not _CONFIRMED_SOURCE_HINT.search(note):
            issues.append(
                "confirmed 须写明来源（用户/对话/owner_config/配额等）"
            )
    elif status == "assumed":
        if len(note) < 8 or not _FALLBACK_HINT.search(note):
            issues.append(
                "assumed 必须含可执行降级方案（fallback/子集/pilot/单模型/抽检等）"
            )
    elif status == "deferred":
        if not _DEFERRED_CORE_HINT.search(note):
            issues.append(
                "deferred 须声明不进入核心 falsifier / 非核心依赖"
            )

    return {
        "kind": label,
        "status": status or None,
        "passed": len(issues) == 0,
        "issues": issues,
        "preview": (row.get("raw") or note)[:160],
    }


def assess_resource_feasibility(
    *,
    plan: str,
    prereg: str = "",
    overview: str = "",
    claim_texts: list[str] | None = None,
    force: bool | None = None,
) -> dict[str, Any]:
    claims = list(claim_texts or [])
    detect_corpus = (plan, prereg, overview, *claims)
    detected = detect_resource_commitments(*detect_corpus)
    applicable = bool(force) if force is not None else bool(detected)

    protocol = (
        extract_resource_feasibility(plan)
        or extract_resource_feasibility(prereg)
    )
    extra = " ".join(
        filter(
            None,
            [
                protocol.get("confirmed_sources"),
                protocol.get("impact_on_core"),
                protocol.get("status_and_fallback"),
            ],
        )
    )
    rows = _parse_commitment_rows(
        protocol.get("commitments")
        or protocol.get("status_and_fallback")
        or ""
    )
    # Allow fallback / deferred wording living in sibling subsections
    if extra:
        for r in rows:
            r["note"] = ((r.get("note") or "") + " " + extra).strip()

    if not applicable:
        return {
            "applicable": False,
            "passed": True,
            "heavy_commitment_detected": False,
            "detected_kinds": [],
            "missing_kinds": [],
            "n_rows": len(rows),
            "per_row": [],
            "reason": "未检测到高成本资源承诺：跳过 resource_feasibility 门禁",
            "has_section": bool(protocol),
        }

    issues: list[str] = []
    if not protocol:
        issues.append(
            "检测到高成本资源承诺但缺少 ## resource_feasibility 段"
        )
    elif not rows:
        issues.append(
            "resource_feasibility 缺少 commitments 清单"
            "（表或 bullet：kind / quantity / status / source_or_fallback）"
        )

    missing_kinds = [
        kind
        for kind in detected
        if not any(_row_covers_kind(r, kind) for r in rows)
    ]
    if missing_kinds:
        issues.append(
            "承诺清单未覆盖已检测到的资源类型: " + ", ".join(missing_kinds)
        )

    per_row = [assess_commitment_row(r) for r in rows]
    for r in per_row:
        if not r["passed"]:
            issues.append(f"{r['kind']}: {'; '.join(r['issues'][:2])}")

    passed = len(issues) == 0
    if passed:
        reason = (
            f"高成本承诺已审查（{', '.join(detected) or 'forced'}）；"
            f"{len(rows)} 条均 confirmed/assumed+降级/deferred 非核心"
        )
    else:
        reason = (
            "资源承诺超出可执行性确认："
            + "；".join(issues[:6])
            + "。补 ## resource_feasibility 后再预注册。"
        )

    return {
        "applicable": True,
        "passed": passed,
        "heavy_commitment_detected": bool(detected),
        "detected_kinds": detected,
        "missing_kinds": missing_kinds,
        "n_rows": len(rows),
        "per_row": per_row,
        "reason": reason,
        "has_section": bool(protocol),
        "issues": issues,
    }


def _read_latest_content(state: State, artifact_type: str) -> str:
    arts = state.list_artifacts(artifact_type)
    if not arts:
        return ""
    rec = state.read_artifact(arts[-1]["id"]) or {}
    return str(rec.get("content") or "")


def _claim_texts_from_transcript(state: State) -> list[str]:
    """当前承诺的正文 —— 读账本，不回放 transcript。

    函数名保留（调用点多），实现整体换掉：原来扫历次 create_claim /
    create_claim 调用且不去重，同一条承诺修几次就被算几次。
    """
    return committed_claim_texts(state)

def collect_resource_feasibility_inputs(state: State) -> dict[str, Any]:
    return {
        "plan": _read_latest_content(state, "research_plan"),
        "prereg": _read_latest_content(state, "pre_registration"),
        "overview": _read_latest_content(state, "hypothesis_research_overview"),
        "claim_texts": _claim_texts_from_transcript(state),
    }


async def _audit_resource_feasibility(
    state: State,
    force: bool | None = None,
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    inputs = collect_resource_feasibility_inputs(state)
    if not inputs["plan"] and not inputs["prereg"]:
        return {
            "status": "error",
            "passed": False,
            "error": "缺少 research_plan / pre_registration，无法审计资源可执行性",
        }

    ni = state.hook_state.get("node_inputs") or {}
    if isinstance(ni, dict) and force is None and "require_resource_feasibility" in ni:
        force = bool(ni.get("require_resource_feasibility"))

    report = assess_resource_feasibility(
        plan=inputs["plan"],
        prereg=inputs["prereg"],
        overview=inputs["overview"],
        claim_texts=inputs["claim_texts"],
        force=force,
    )

    artifact_id: str | None = None
    if save_report:
        body = {**report}
        lines = [
            "# Resource Feasibility Audit",
            "",
            f"- **passed**: {report['passed']}",
            f"- applicable: {report['applicable']}",
            f"- detected_kinds: {report.get('detected_kinds')}",
            f"- missing_kinds: {report.get('missing_kinds')}",
            "",
            report["reason"],
            "",
            "## Per commitment",
        ]
        for row in report.get("per_row") or []:
            mark = "✓" if row["passed"] else "✗"
            extra = (" — " + "; ".join(row["issues"])) if row.get("issues") else ""
            lines.append(
                f"- {mark} **{row.get('kind')}** (status={row.get('status')}){extra}"
            )
        content = (
            "\n".join(lines)
            + "\n\n```json\n"
            + json.dumps(body, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
        saved = save_hypothesis_singleton(
            state,
            "hypothesis_resource_feasibility",
            "Resource_Feasibility",
            content,
            metadata={
                "passed": report["passed"],
                "applicable": report["applicable"],
                "detected_kinds": report.get("detected_kinds") or [],
                "missing_kinds": report.get("missing_kinds") or [],
            },
        )
        artifact_id = saved["id"]

    state.hook_state["last_resource_feasibility"] = report
    return {
        "status": "success",
        "passed": report["passed"],
        "artifact_id": artifact_id,
        "message": report["reason"],
        **report,
    }


register_tool(
    ToolDefinition(
        name="audit_resource_feasibility",
        description=(
            "审计高成本资源承诺是否可执行。\n\n"
            "检测到多模型 / 多人标注 / 大规模人工审计 / 多个完整 benchmark 时，"
            "research_plan 或 prereg 须含 ## resource_feasibility：\n"
            "- commitments 表/清单：kind、quantity、status、source_or_fallback\n"
            "- status=confirmed → 写明用户/对话/配置来源\n"
            "- status=assumed → 必须有降级方案\n"
            "- status=deferred → 声明不进核心 falsifier\n\n"
            "**Use when**：save plan/prereg 草稿后、**freeze_artifact 之前**。\n"
            "passed=false → 补可行性段或降级后再 freeze。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "强制按资源门禁检查；默认自动检测高成本承诺",
                },
                "save_report": {"type": "boolean", "default": True},
            },
        },
        allowed_node_types=["hypothesis"],
    ),
    _audit_resource_feasibility,
)
