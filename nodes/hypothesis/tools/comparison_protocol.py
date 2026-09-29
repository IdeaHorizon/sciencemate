"""audit_comparison_protocol — 跨任务/异构 benchmark 比较必须先冻结可比协议。

基础字段：共同样本、共同分母、可比较操作、统一故障处理、
**unified_definitions**（凡指标/定义类构造须审查是否跨侧统一）。
异构 benchmark / 操作比例对比时另强制：
  - behavior_alignment（跨数据集行为映射表）
禁止把未对齐的操作族（如 API/reasoning vs 点击/输入）直接比比例。
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State

from ..committed import committed_claim_texts
from core.tool_registry import ToolDefinition, register_tool

from .artifact_save import save_hypothesis_singleton

_BASE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "common_sample",
        (
            "common_sample", "common samples", "shared sample",
            "共同样本", "共用样本", "共享样本", "共同任务集",
        ),
    ),
    (
        "common_denominator",
        (
            "common_denominator", "shared denominator",
            "共同分母", "分母定义", "统一分母",
        ),
    ),
    (
        "comparable_ops",
        (
            "comparable_ops", "comparable operations", "comparable_op_types",
            "可比较操作", "可比较操作类型", "可比操作", "操作类型可比",
        ),
    ),
    (
        "env_failure_policy",
        (
            "env_failure_policy", "environment failure", "failure handling",
            "环境故障", "故障处理", "统一故障", "失败处理策略",
        ),
    ),
    (
        "unified_definitions",
        (
            "unified_definitions", "definition_unification", "metric_unification",
            "construct_unification", "shared_definitions",
            "复杂度定义", "complexity_definition", "complexity definition",
            "complexity_norm", "complexity rubric",
            "指标统一", "定义统一", "定义对齐", "统一定义", "指标定义",
            "metric definition", "definitions inventory",
        ),
    ),
)

_HETERO_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "behavior_alignment",
        (
            "behavior_alignment", "action alignment", "op_mapping",
            "behavior mapping", "cross_benchmark_mapping",
            "行为对齐", "行为映射", "操作映射", "跨数据集映射", "动作对齐",
        ),
    ),
)

_PROTOCOL_FIELDS = _BASE_FIELDS + _HETERO_FIELDS

_CROSS_TASK_HINT = re.compile(
    r"(?:跨任务|跨场景|跨设定|多任务对比|任务间比较|场景对比|"
    r"cross[\s_-]?task|cross[\s_-]?scenario|across\s+tasks?|"
    r"between\s+tasks?|vs\.?\s+|versus\s+|对比\s*(?:任务|场景|条件))",
    flags=re.IGNORECASE,
)

_HETERO_BENCH_HINT = re.compile(
    r"(?:τ[\s_-]?bench|tau[\s_-]?bench|webarena|mind2web|agenteval|"
    r"osworld|appworld|toolbench|swe[\s_-]?bench|"
    r"跨\s*(?:benchmark|数据集|benchmark)|"
    r"(?:多个|不同)\s*(?:benchmark|数据集|测试集)|"
    r"异构\s*(?:benchmark|数据集|操作)|"
    r"跨域\s*(?:对比|比较))",
    flags=re.IGNORECASE,
)

_CONSTRUCT_COMPARE_HINT = re.compile(
    r"(?:复杂度|complexity)\s*(?:分布|比例|对比|比较|归一)|"
    r"(?:相同|同一)\s*复杂度|"
    r"操作(?:类型)?比例|"
    r"(?:behavior|action|operation)\s*(?:mix|ratio|proportion)|"
    r"各(?:类)?操作占比|"
    r"(?:成功率|准确率|latency|时延|成本|token|reward|得分|指标)\s*"
    r"(?:对比|比较|分布|比例|差异)|"
    r"(?:metric|definition|taxonomy|成功标准|验收标准)\s*"
    r"(?:对比|比较|对齐|统一)|"
    r"跨(?:侧|集|域).*(?:指标|定义|标准)",
    flags=re.IGNORECASE,
)

_API_REASONING_SIDE = re.compile(
    r"(?:reasoning|thought|api\s*调用|api\s*call|tool\s*call|"
    r"客服\s*api|函数调用|tool[\s_-]?use)",
    flags=re.IGNORECASE,
)
_UI_NAV_SIDE = re.compile(
    r"(?:点击|输入|滚动|填表|网页导航|click|type|scroll|navigate|"
    r"hover|key\s*press|dom)",
    flags=re.IGNORECASE,
)

_MAPPING_HINT = re.compile(
    r"(?:↔|→|->|=>|映射|对齐|对应|等价|归入|maps?\s*to|correspond|"
    r"aligned|alignment\s*table|mapping\s*table)",
    flags=re.IGNORECASE,
)

_CONSTRUCT_KIND_HINT = re.compile(
    r"(?:指标|metric|定义|definition|成功率|准确率|precision|recall|"
    r"latency|时延|成本|cost|token|复杂度|complexity|类别|taxonomy|"
    r"阈值|threshold|分母|denominator|成功标准|验收|reward|得分|score|"
    r"操作类型|action\s*type)",
    flags=re.IGNORECASE,
)

_UNIFY_STANCE_HINT = re.compile(
    r"(?:统一|归一|同一定义|共享定义|无需统一|不必统一|各自保留|"
    r"对齐定义|shared\s+definition|unify|unif(?:y|ied)|normalize|"
    r"not\s+needed|n/?a\s*[-:：])",
    flags=re.IGNORECASE,
)

_VAGUE = re.compile(
    r"^(?:tbd|todo|待定|同上|见上|n/?a|合适|合理|适当|默认)[.。]?$",
    flags=re.IGNORECASE,
)


def looks_like_cross_task_comparison(*texts: str) -> bool:
    blob = "\n".join(t for t in texts if t)
    if not blob.strip():
        return False
    if _CROSS_TASK_HINT.search(blob):
        return True
    if re.search(r"任务\s*[A-Ba-b12一二]", blob) and re.search(
        r"任务\s*[A-Ba-b12一二].*(?:对比|比较|vs)", blob, flags=re.IGNORECASE
    ):
        return True
    return False


def looks_like_hetero_benchmark(*texts: str) -> bool:
    blob = "\n".join(t for t in texts if t)
    if not blob.strip():
        return False
    if _HETERO_BENCH_HINT.search(blob):
        return True
    names = re.findall(
        r"(?:τ[\s_-]?bench|tau[\s_-]?bench|webarena|mind2web|osworld|"
        r"appworld|toolbench|swe[\s_-]?bench)",
        blob,
        flags=re.IGNORECASE,
    )
    uniq = {re.sub(r"[\s_-]+", "", n.lower()) for n in names}
    return len(uniq) >= 2


def looks_like_complexity_comparison(*texts: str) -> bool:
    """Backward-compatible alias."""
    return looks_like_construct_comparison(*texts)


def looks_like_construct_comparison(*texts: str) -> bool:
    blob = "\n".join(t for t in texts if t)
    return bool(blob and _CONSTRUCT_COMPARE_HINT.search(blob))


def looks_like_unaligned_action_mix(*texts: str) -> bool:
    blob = "\n".join(t for t in texts if t)
    if not blob.strip():
        return False
    return bool(_API_REASONING_SIDE.search(blob) and _UI_NAV_SIDE.search(blob))


def _norm_key(text: str) -> str:
    return re.sub(r"[\s_\-]+", "", (text or "").lower())


def _field_aliases_norm() -> dict[str, str]:
    out: dict[str, str] = {}
    for canonical, aliases in _PROTOCOL_FIELDS:
        for a in aliases:
            out[_norm_key(a)] = canonical
    return out


def extract_comparison_protocol(content: str) -> dict[str, str]:
    if not content:
        return {}
    alias_map = _field_aliases_norm()
    found: dict[str, str] = {}

    section = content
    m = re.search(
        r"(?:^|\n)#{1,2}\s*comparison[_\s-]?protocol\s*\n(.*?)(?=\n#{1,2}\s+\S|\Z)",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        m = re.search(
            r"(?:^|\n)#{1,2}\s*比较协议\s*\n(.*?)(?=\n#{1,2}\s+\S|\Z)",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if m:
        section = m.group(1)

    for hm in re.finditer(
        r"(?:^|\n)#{2,4}\s*([^\n]+)\n([\s\S]*?)(?=\n#{2,4}\s|\Z)",
        section,
    ):
        title = hm.group(1).strip()
        body = hm.group(2).strip()
        key = alias_map.get(_norm_key(title))
        if key and body:
            prev = found.get(key, "")
            merged = re.sub(r"\s+", " ", body)[:800]
            if len(merged) > len(prev):
                found[key] = merged

    for lm in re.finditer(
        r"(?:^|\n)\s*(?:[-*]|\d+[\.\)、])?\s*"
        r"[「\"']?([^：:\n]{2,40})[」\"']?\s*[:：]\s*([^\n]{3,500})",
        section,
    ):
        label = lm.group(1).strip()
        body = lm.group(2).strip()
        key = alias_map.get(_norm_key(label))
        if key and body and len(body) > len(found.get(key, "")):
            found[key] = body

    return found


def _field_ok(value: str | None, *, min_len: int = 8) -> bool:
    if not value or not str(value).strip():
        return False
    text = str(value).strip()
    if len(text) < min_len:
        return False
    if _VAGUE.match(text):
        return False
    return True


def _unified_definitions_ok(value: str | None) -> tuple[bool, str | None]:
    if not _field_ok(value, min_len=24):
        return False, "unified_definitions 缺失或过短（须盘点指标/定义并说明是否统一）"
    text = str(value)
    if not _CONSTRUCT_KIND_HINT.search(text):
        return False, (
            "unified_definitions 未点名任何指标/定义类构造"
            "（如成功率、阈值、复杂度、类别、成功标准等）"
        )
    if not _UNIFY_STANCE_HINT.search(text):
        return False, "unified_definitions 未给出统一/归一/无需统一等审查结论"
    return True, None


def _behavior_alignment_ok(value: str | None) -> tuple[bool, str | None]:
    if not _field_ok(value, min_len=24):
        return False, "behavior_alignment 缺失或过短"
    text = str(value)
    if not _MAPPING_HINT.search(text):
        return False, "behavior_alignment 未见映射/对齐表（需含 ↔/→/映射/对齐 等）"
    sides = 0
    if _API_REASONING_SIDE.search(text) or re.search(
        r"(?:api|tool|call|reasoning)", text, flags=re.IGNORECASE
    ):
        sides += 1
    if _UI_NAV_SIDE.search(text) or re.search(
        r"(?:click|type|ui|nav|web)", text, flags=re.IGNORECASE
    ):
        sides += 1
    if re.search(r"(?:benchmark|数据集|τ|tau|webarena)", text, flags=re.IGNORECASE):
        sides += 1
    if sides < 1:
        return False, "behavior_alignment 未写清跨侧操作如何对应"
    return True, None


def _strip_protocol_sections(text: str) -> str:
    """Ignore protocol body when detecting comparison intent (avoid self-trigger)."""
    if not text:
        return ""
    out = re.sub(
        r"(?:^|\n)#{1,2}\s*(?:comparison[_\s-]?protocol|比较协议)\s*\n.*?(?=\n#{1,2}\s+\S|\Z)",
        "\n",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return out


def assess_comparison_protocol(
    *,
    plan: str,
    prereg: str = "",
    overview: str = "",
    claim_texts: list[str] | None = None,
    force_cross_task: bool | None = None,
    force_hetero: bool | None = None,
) -> dict[str, Any]:
    claims = list(claim_texts or [])
    # Detection corpus excludes protocol sections to avoid self-matching
    # phrases like「成功率对比」inside the protocol template.
    detect_corpus = (
        _strip_protocol_sections(plan),
        _strip_protocol_sections(prereg),
        _strip_protocol_sections(overview),
        *claims,
    )

    cross = (
        bool(force_cross_task)
        if force_cross_task is not None
        else looks_like_cross_task_comparison(*detect_corpus)
    )
    construct_cmp = looks_like_construct_comparison(*detect_corpus)
    hetero = (
        bool(force_hetero)
        if force_hetero is not None
        else (
            looks_like_hetero_benchmark(*detect_corpus)
            or construct_cmp
            or looks_like_unaligned_action_mix(*detect_corpus)
        )
    )
    if hetero:
        cross = True

    protocol = extract_comparison_protocol(plan) or extract_comparison_protocol(prereg)

    required = [c for c, _ in _BASE_FIELDS]
    if hetero:
        required.extend(c for c, _ in _HETERO_FIELDS)

    per_field: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    field_issues: list[str] = []

    for canonical, _aliases in _PROTOCOL_FIELDS:
        val = protocol.get(canonical)
        if canonical == "unified_definitions" and canonical in required:
            ok, issue = _unified_definitions_ok(val)
        elif canonical == "behavior_alignment" and canonical in required:
            ok, issue = _behavior_alignment_ok(val)
        elif canonical in required:
            ok = _field_ok(val)
            issue = None if ok else f"{canonical} 缺失或空话"
        else:
            ok = _field_ok(val) if val else True
            issue = None
        per_field[canonical] = {
            "present": bool(val),
            "ok": ok if canonical in required else (True if not val else ok),
            "required": canonical in required,
            "value_preview": (val or "")[:200],
        }
        if canonical in required and not ok:
            missing.append(canonical)
            if issue:
                field_issues.append(issue)

    unaligned_mix = looks_like_unaligned_action_mix(*detect_corpus)
    alignment_ok, _ = _behavior_alignment_ok(protocol.get("behavior_alignment"))
    naive_cross_taxonomy = bool(unaligned_mix and not alignment_ok)

    if not cross and not hetero:
        return {
            "applicable": False,
            "passed": True,
            "cross_task_detected": False,
            "hetero_benchmark_detected": False,
            "construct_comparison_detected": construct_cmp,
            "complexity_comparison_detected": construct_cmp,
            "naive_cross_taxonomy": False,
            "missing_fields": [],
            "field_issues": [],
            "protocol": protocol,
            "per_field": per_field,
            "reason": "未检测到跨任务/异构/指标定义横比：跳过 comparison_protocol 门禁",
        }

    passed = not missing and not naive_cross_taxonomy
    parts: list[str] = []
    if missing:
        parts.append("缺失或不合格: " + ", ".join(missing))
    if field_issues:
        parts.append("；".join(field_issues[:4]))
    if naive_cross_taxonomy:
        parts.append(
            "检测到 API/reasoning 与 点击/输入/导航 混比，但无合格 behavior_alignment；"
            "禁止未对齐就比较操作比例或任何跨侧指标"
        )

    if passed:
        reason = (
            "比较协议齐全（含 unified_definitions：指标/定义类已审查是否统一）"
            + ("；异构行为已对齐" if hetero else "")
        )
    else:
        reason = (
            "比较协议不完整：指标/定义类须审查是否统一，异构操作须行为对齐。"
            + "；".join(parts)
            + "。补 ## comparison_protocol 后再做跨侧对比。"
        )

    return {
        "applicable": True,
        "passed": passed,
        "cross_task_detected": cross,
        "hetero_benchmark_detected": hetero,
        "construct_comparison_detected": construct_cmp,
        "complexity_comparison_detected": construct_cmp,
        "naive_cross_taxonomy": naive_cross_taxonomy,
        "missing_fields": missing,
        "field_issues": field_issues,
        "required_fields": required,
        "protocol": protocol,
        "per_field": per_field,
        "reason": reason,
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

def collect_comparison_audit_inputs(state: State) -> dict[str, Any]:
    return {
        "plan": _read_latest_content(state, "research_plan"),
        "prereg": _read_latest_content(state, "pre_registration"),
        "overview": _read_latest_content(state, "hypothesis_research_overview"),
        "claim_texts": _claim_texts_from_transcript(state),
    }


async def _audit_comparison_protocol(
    state: State,
    force_cross_task: bool | None = None,
    force_hetero: bool | None = None,
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    inputs = collect_comparison_audit_inputs(state)
    if not inputs["plan"] and not inputs["prereg"]:
        return {
            "status": "error",
            "passed": False,
            "error": "缺少 research_plan / pre_registration，无法审计比较协议",
        }

    ni = state.hook_state.get("node_inputs") or {}
    if isinstance(ni, dict):
        if force_cross_task is None and "require_comparison_protocol" in ni:
            force_cross_task = bool(ni.get("require_comparison_protocol"))
        if force_hetero is None and "require_hetero_alignment" in ni:
            force_hetero = bool(ni.get("require_hetero_alignment"))

    report = assess_comparison_protocol(
        plan=inputs["plan"],
        prereg=inputs["prereg"],
        overview=inputs["overview"],
        claim_texts=inputs["claim_texts"],
        force_cross_task=force_cross_task,
        force_hetero=force_hetero,
    )

    artifact_id: str | None = None
    if save_report:
        body = {**report}
        lines = [
            "# Comparison Protocol Audit",
            "",
            f"- **passed**: {report['passed']}",
            f"- applicable: {report['applicable']}",
            f"- cross_task_detected: {report['cross_task_detected']}",
            f"- hetero_benchmark_detected: {report.get('hetero_benchmark_detected')}",
            f"- construct_comparison_detected: {report.get('construct_comparison_detected')}",
            f"- naive_cross_taxonomy: {report.get('naive_cross_taxonomy')}",
            "",
            report["reason"],
            "",
            "## Fields",
        ]
        for key, meta in (report.get("per_field") or {}).items():
            req = "必填" if meta.get("required") else "可选"
            mark = "✓" if meta.get("ok") else "✗"
            preview = meta.get("value_preview") or "(missing)"
            lines.append(f"- {mark} **{key}** ({req}): {preview}")
        content = (
            "\n".join(lines)
            + "\n\n```json\n"
            + json.dumps(body, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
        saved = save_hypothesis_singleton(
            state,
            "hypothesis_comparison_protocol",
            "Comparison_Protocol",
            content,
            metadata={
                "passed": report["passed"],
                "applicable": report["applicable"],
                "missing_fields": report.get("missing_fields") or [],
                "hetero_benchmark_detected": report.get("hetero_benchmark_detected"),
                "naive_cross_taxonomy": report.get("naive_cross_taxonomy"),
            },
        )
        artifact_id = saved["id"]

    state.hook_state["last_comparison_protocol"] = report

    return {
        "status": "success",
        "passed": report["passed"],
        "artifact_id": artifact_id,
        "message": report["reason"],
        **report,
    }


register_tool(
    ToolDefinition(
        name="audit_comparison_protocol",
        description=(
            "审计跨任务/异构比较协议。\n\n"
            "**基础必填**：common_sample / common_denominator / comparable_ops / "
            "env_failure_policy / **unified_definitions**\n"
            "（凡指标、阈值、成功标准、复杂度、类别等定义类构造，须审查是否跨侧统一）。\n"
            "**异构或操作比例对比另强求**：behavior_alignment（跨数据集映射表）。\n\n"
            "API/reasoning 与 点击/输入 未对齐就比 → fail。\n"
            "**Use when**：save research_plan 后、**freeze_artifact 之前**。\n"
            "passed=false → 补协议后重跑；禁止带着缺口 freeze。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "force_cross_task": {
                    "type": "boolean",
                    "description": "强制按跨任务门禁检查；默认自动检测",
                },
                "force_hetero": {
                    "type": "boolean",
                    "description": "强制要求行为映射；默认自动检测",
                },
                "save_report": {"type": "boolean", "default": True},
            },
        },
        allowed_node_types=["hypothesis"],
    ),
    _audit_comparison_protocol,
)
