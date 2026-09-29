"""audit_cost_instrumentation — 步骤级计算成本假设必须有数据采集方案。

假说若要求分析 per-step / 步骤级计算开销（token、GPU-h、墙钟、API 费用等），
仅有 resource_estimates 粗估不够：research_plan 须含 ## cost_instrumentation，
写明度量定义、挂接 Step ID、采集方法、聚合方式与缺失策略。
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from .artifact_save import save_hypothesis_singleton

_REQUIRED_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "metric_definition",
        (
            "metric_definition", "cost_metric", "度量定义", "成本定义",
            "metric definition", "what_is_measured", "计费口径",
        ),
    ),
    (
        "collection_points",
        (
            "collection_points", "instrumentation_points", "step_mapping",
            "采集点", "采集步骤", "步骤映射", "step ids", "挂接步骤",
        ),
    ),
    (
        "collection_method",
        (
            "collection_method", "instrumentation", "telemetry",
            "采集方法", "采集方式", "埋点", "日志方案", "profiler",
        ),
    ),
    (
        "aggregation",
        (
            "aggregation", "rollup", "aggregate",
            "聚合", "汇总", "对齐 falsifier", "如何汇总",
        ),
    ),
    (
        "missing_policy",
        (
            "missing_policy", "missing_data", "gap_policy",
            "缺失策略", "缺失处理", "无日志", "采集失败",
        ),
    ),
)

_SECTION_ALIASES = (
    "cost_instrumentation",
    "cost instrumentation",
    "compute_cost_instrumentation",
    "cost_collection",
    "成本采集",
    "开销采集",
    "计算成本采集",
    "步骤成本采集",
)

_STEP_COST_HINT = re.compile(
    r"(?:步骤级|逐步|逐\s*step|per[\s_-]?step|step[\s_-]?level|"
    r"各步骤|每一步|按步骤|按\s*step)\s*"
    r"(?:计算)?(?:成本|开销|费用|耗时|时延|latency|token|GPU|"
    r"算力|墙钟|wall[\s_-]?clock|FLOP)|"
    r"(?:成本|开销|token\s+cost|computational\s+cost|gpu[\s_-]?h?|"
    r"墙钟|wall[\s_-]?clock|latency).{0,40}"
    r"(?:步骤|per[\s_-]?step|step[\s_-]?level|逐步)|"
    r"(?:步骤|per[\s_-]?step|step[\s_-]?level|逐步).{0,40}"
    r"(?:成本|开销|token|latency|gpu|算力|费用)|"
    r"成本分解|cost\s+breakdown|cost_per_step|step_cost|步骤成本|"
    r"分析步骤级计算成本|步骤级计算成本",
    flags=re.IGNORECASE,
)

_COST_METRIC_HINT = re.compile(
    r"(?:token\s+cost|gpu[\s_-]?hours?|wall[\s_-]?time|"
    r"计算开销|步骤开销|per[\s_-]?step\s+cost|"
    r"cost_per_step|step_cost|步骤成本|步骤级.*(?:成本|开销))",
    flags=re.IGNORECASE,
)

_METHOD_SUBSTANCE = re.compile(
    r"(?:log|日志|trace|telemetry|profiler|nvprof|nsys|wandb|"
    r"mlflow|callback|hook|meter|计费|billing|usage\s*api|"
    r"tiktoken|openai\.usage|cloud\s*monitoring|prometheus|"
    r"time\.perf|cuda\s*event|埋点|采集|记录|导出)",
    flags=re.IGNORECASE,
)

_ESTIMATE_ONLY = re.compile(
    r"(?:粗估|估算|estimate|resource_estimates|预算|预计)",
    flags=re.IGNORECASE,
)

_STEP_ID_RE = re.compile(r"\bS\d+[a-z]?\b", flags=re.IGNORECASE)


def _strip_instrumentation_sections(text: str) -> str:
    if not text:
        return ""
    return re.sub(
        r"(?:^|\n)#{1,2}\s*(?:cost[_\s-]?instrumentation|cost[_\s-]?collection|"
        r"成本采集|开销采集|计算成本采集|步骤成本采集)\s*\n"
        r".*?(?=\n#{1,2}\s+\S|\Z)",
        "\n",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )


def looks_like_step_cost_analysis(*texts: str) -> bool:
    blob = "\n".join(_strip_instrumentation_sections(t or "") for t in texts)
    if not blob.strip():
        return False
    return bool(_STEP_COST_HINT.search(blob) or _COST_METRIC_HINT.search(blob))


def extract_cost_instrumentation(content: str) -> dict[str, str]:
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
    parts = re.split(r"(?:^|\n)###\s+", section)
    if len(parts) <= 1:
        # Flat body — try to map whole section to all required via keywords
        fields["metric_definition"] = section.strip()
        return fields

    for block in parts[1:]:
        lines = block.strip().splitlines()
        if not lines:
            continue
        key = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        canon = _canonicalize_field(key)
        if canon:
            fields[canon] = body
        else:
            fields[key.lower()] = body
    return fields


def _canonicalize_field(key: str) -> str | None:
    k = key.strip().lower().replace("-", "_").replace(" ", "_")
    for canon, aliases in _REQUIRED_FIELDS:
        if k == canon:
            return canon
        for a in aliases:
            an = a.lower().replace(" ", "_").replace("-", "_")
            if k == an or a.lower() in key.lower():
                return canon
    return None


def _field_ok(name: str, value: str) -> tuple[bool, str]:
    text = (value or "").strip()

    if name == "collection_points":
        steps = _STEP_ID_RE.findall(text)
        if not steps:
            return False, "未挂接 computational_workflow Step ID（如 S1, S2）"
        return True, f"steps={sorted(set(s.upper() for s in steps))}"

    if len(text) < 12:
        return False, "过短或缺失"

    if name == "metric_definition":
        if not re.search(
            r"(?:token|GPU|gpu|墙钟|wall|latency|时延|美元|USD|¥|元|"
            r"FLOP|小时|second|秒|毫秒|ms|cost|开销|费用|单位)",
            text,
            flags=re.IGNORECASE,
        ):
            return False, "未写清成本度量/单位"
        return True, text[:120]

    if name == "collection_method":
        if _ESTIMATE_ONLY.search(text) and not _METHOD_SUBSTANCE.search(text):
            return False, "只有估算语言，缺少可执行采集方法（日志/profiler/usage API 等）"
        if not _METHOD_SUBSTANCE.search(text):
            return False, "未写明可执行采集方法（日志/profiler/usage API/埋点等）"
        return True, text[:120]

    if name == "aggregation":
        if not re.search(
            r"(?:汇总|聚合|sum|mean|平均|按\s*step|per[\s_-]?step|"
            r"falsifier|证伪|对齐|rollup|表)",
            text,
            flags=re.IGNORECASE,
        ):
            return False, "未说明如何汇总到 falsifier / 步骤表"
        return True, text[:120]

    if name == "missing_policy":
        if not re.search(
            r"(?:剔除|重试|失败|记\s*NA|插补|impute|exclude|fail|"
            r"对照|一致|缺失|missing|跳过|abort)",
            text,
            flags=re.IGNORECASE,
        ):
            return False, "未写缺失/无日志时的处理策略"
        return True, text[:120]

    return len(text) >= 12, text[:120]


def _workflow_step_ids(plan: str) -> set[str]:
    """Step IDs from computational_workflow / mermaid+task table only."""
    if not plan:
        return set()
    m = re.search(
        r"(?:^|\n)#{1,3}\s*(?:computational[_\s-]?workflow|计算工作流|计算流程)\s*\n"
        r"(.*?)(?=\n#{1,2}\s+\S|\Z)",
        plan,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        return {s.upper() for s in _STEP_ID_RE.findall(m.group(1))}
    # Fallback: strip protocol sections then scan
    stripped = plan
    for alias in (
        "cost_instrumentation",
        "cost instrumentation",
        "resource_feasibility",
        "comparison_protocol",
        "成本采集",
        "开销采集",
        "资源可行性",
    ):
        stripped = re.sub(
            r"(?:^|\n)#{1,2}\s*"
            + re.escape(alias)
            + r"\s*\n.*?(?=\n#{1,2}\s+\S|\Z)",
            "\n",
            stripped,
            flags=re.IGNORECASE | re.DOTALL,
        )
    return {s.upper() for s in _STEP_ID_RE.findall(stripped)}


def assess_cost_instrumentation(
    *,
    plan: str,
    prereg: str = "",
    overview: str = "",
    claim_texts: list[str] | None = None,
    force: bool | None = None,
) -> dict[str, Any]:
    claims = list(claim_texts or [])
    detect_corpus = (plan, prereg, overview, *claims)
    applicable = (
        bool(force) if force is not None else looks_like_step_cost_analysis(*detect_corpus)
    )

    protocol = (
        extract_cost_instrumentation(plan)
        or extract_cost_instrumentation(prereg)
    )

    if not applicable:
        return {
            "applicable": False,
            "passed": True,
            "step_cost_detected": False,
            "missing_fields": [],
            "per_field": {},
            "reason": "未检测到步骤级/计算成本分析诉求：跳过 cost_instrumentation 门禁",
            "has_section": bool(protocol),
            "unmapped_steps": [],
        }

    per_field: dict[str, Any] = {}
    missing: list[str] = []
    issues: list[str] = []

    if not protocol:
        issues.append(
            "检测到步骤级/计算成本分析诉求，但缺少 ## cost_instrumentation 段"
            "（仅有 resource_estimates 粗估不够）"
        )

    for name, _aliases in _REQUIRED_FIELDS:
        value = protocol.get(name) or ""
        ok, preview = _field_ok(name, value)
        per_field[name] = {
            "required": True,
            "ok": ok,
            "value_preview": preview if value else "(missing)",
        }
        if not ok:
            missing.append(name)
            issues.append(f"{name}: {preview if not value else preview}")

    # Step IDs in instrumentation should exist in workflow when plan has steps
    wf_steps = _workflow_step_ids(plan)
    cited = {
        s.upper()
        for s in _STEP_ID_RE.findall(protocol.get("collection_points") or "")
    }
    unmapped = sorted(cited - wf_steps) if wf_steps and cited else []
    if unmapped:
        issues.append(
            "collection_points 含 workflow 中不存在的 Step ID: "
            + ", ".join(unmapped)
        )

    passed = len(issues) == 0
    if passed:
        reason = (
            "步骤级成本采集方案齐全（度量定义/采集点/方法/聚合/缺失策略）"
            + (f"；挂接 {sorted(cited)}" if cited else "")
        )
    else:
        reason = (
            "缺少计算开销采集方案："
            + "；".join(issues[:6])
            + "。补 ## cost_instrumentation 后再以成本证伪。"
        )

    return {
        "applicable": True,
        "passed": passed,
        "step_cost_detected": True,
        "missing_fields": missing,
        "per_field": per_field,
        "reason": reason,
        "has_section": bool(protocol),
        "unmapped_steps": unmapped,
        "issues": issues,
    }


def _read_latest_content(state: State, artifact_type: str) -> str:
    arts = state.list_artifacts(artifact_type)
    if not arts:
        return ""
    rec = state.read_artifact(arts[-1]["id"]) or {}
    return str(rec.get("content") or "")


def _claim_texts_from_transcript(state: State) -> list[str]:
    texts: list[str] = []
    if not state.transcript_path.exists():
        return texts
    for line in state.transcript_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") != "tool_call":
            continue
        name = e.get("name")
        args = e.get("args") or {}
        if name in ("create_claim",):
            t = (
                args.get("hypothesis_text")
                or args.get("claim_text")
                or args.get("text")
                or ""
            ).strip()
            if t:
                texts.append(t)
            fs = args.get("falsification_criteria_structured")
            if isinstance(fs, dict):
                for key in ("metric", "dataset", "regime"):
                    v = fs.get(key)
                    if v:
                        texts.append(str(v))
                tr = fs.get("threshold_rationale")
                if isinstance(tr, dict):
                    texts.extend(str(v) for v in tr.values() if v)
                elif isinstance(tr, str) and tr:
                    texts.append(tr)
    return texts


def collect_cost_instrumentation_inputs(state: State) -> dict[str, Any]:
    return {
        "plan": _read_latest_content(state, "research_plan"),
        "prereg": _read_latest_content(state, "pre_registration"),
        "overview": _read_latest_content(state, "hypothesis_research_overview"),
        "claim_texts": _claim_texts_from_transcript(state),
    }


async def _audit_cost_instrumentation(
    state: State,
    force: bool | None = None,
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    inputs = collect_cost_instrumentation_inputs(state)
    if not inputs["plan"] and not inputs["prereg"]:
        return {
            "status": "error",
            "passed": False,
            "error": "缺少 research_plan / pre_registration，无法审计成本采集方案",
        }

    ni = state.hook_state.get("node_inputs") or {}
    if isinstance(ni, dict) and force is None and "require_cost_instrumentation" in ni:
        force = bool(ni.get("require_cost_instrumentation"))

    report = assess_cost_instrumentation(
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
            "# Cost Instrumentation Audit",
            "",
            f"- **passed**: {report['passed']}",
            f"- applicable: {report['applicable']}",
            f"- step_cost_detected: {report.get('step_cost_detected')}",
            f"- missing_fields: {report.get('missing_fields')}",
            f"- unmapped_steps: {report.get('unmapped_steps')}",
            "",
            report["reason"],
            "",
            "## Fields",
        ]
        for key, meta in (report.get("per_field") or {}).items():
            mark = "✓" if meta.get("ok") else "✗"
            lines.append(
                f"- {mark} **{key}**: {meta.get('value_preview') or '(missing)'}"
            )
        content = (
            "\n".join(lines)
            + "\n\n```json\n"
            + json.dumps(body, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
        saved = save_hypothesis_singleton(
            state,
            "hypothesis_cost_instrumentation",
            "Cost_Instrumentation",
            content,
            metadata={
                "passed": report["passed"],
                "applicable": report["applicable"],
                "missing_fields": report.get("missing_fields") or [],
                "unmapped_steps": report.get("unmapped_steps") or [],
            },
        )
        artifact_id = saved["id"]

    state.hook_state["last_cost_instrumentation"] = report
    return {
        "status": "success",
        "passed": report["passed"],
        "artifact_id": artifact_id,
        "message": report["reason"],
        **report,
    }


register_tool(
    ToolDefinition(
        name="audit_cost_instrumentation",
        description=(
            "审计步骤级/计算成本分析是否落实数据采集方案。\n\n"
            "检测到 per-step cost、token/GPU/墙钟、成本分解等诉求时，"
            "research_plan 须含 ## cost_instrumentation：\n"
            "- metric_definition / collection_points（挂 Step ID）/\n"
            "  collection_method / aggregation / missing_policy\n"
            "仅有 resource_estimates 粗估 → fail。\n"
            "**Use when**：save research_plan 后、**freeze_artifact 之前**。\n"
            "passed=false → 补采集方案或改掉成本类 falsifier；禁止带着缺口 freeze。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "强制按成本采集门禁检查；默认自动检测",
                },
                "save_report": {"type": "boolean", "default": True},
            },
        },
        allowed_node_types=["hypothesis"],
    ),
    _audit_cost_instrumentation,
)
