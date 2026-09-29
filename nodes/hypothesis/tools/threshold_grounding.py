"""audit_threshold_grounding — 假说阈值必须有文献/理论/先导依据。

仅有「可机械判定的数字」不够：每个**数值** threshold 须声明来源类型、
可追溯依据，以及达标的科学含义；否则即使实验命中阈值，科学解释也很弱。

定性 / 存在性证伪（comparison=qualitative|exists|not_exists）不要求编数字：
写清可观察判据即可。禁止把「不许编造数字」写成「必须有数字」。
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from ..committed import committed_falsifiers
from .artifact_save import save_hypothesis_singleton

_SOURCE_TYPES = frozenset({
    "literature",
    "theory",
    "pilot",
    "domain_convention",
    "user_specified",
})

_NUMERIC_COMPARISONS = frozenset({">", "<", ">=", "<=", "==", "!="})

_NON_NUMERIC_COMPARISONS = frozenset({
    "qualitative",
    "exists",
    "not_exists",
})

_COMPARISON_ALIASES = {
    "existence": "exists",
    "exist": "exists",
    "observed": "exists",
    "observable": "exists",
    "absent": "not_exists",
    "absence": "not_exists",
    "missing": "not_exists",
    "trend": "qualitative",
    "pattern": "qualitative",
    "monotonic": "qualitative",
}

# Pure numeric threshold values (not free-form criterion text that happens to cite a count)
_PURE_NUMERIC = re.compile(
    r"^\s*[<>]=?\s*-?\d+(?:\.\d+)?\s*%?\s*$|"
    r"^\s*-?\d+(?:\.\d+)?\s*(?:%|[x×倍])?\s*$",
    flags=re.IGNORECASE,
)

_VAGUE = re.compile(
    r"(?:^|\s)(?:显著|足够|经验(?:值|上)?|拍脑袋| arbitrarily|arbitrary|"
    r"looks?\s+good|sufficient(?:ly)?|明显(?:提高|优于)?|大致|大概|左右)"
    r"(?:\s|$)",
    flags=re.IGNORECASE,
)

_RATIO_HINT = re.compile(
    r"(?:\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*[x×倍]|提升\s*\d+|降低\s*\d+|"
    r"ratio|相对|percent)",
    flags=re.IGNORECASE,
)


def _as_rationale(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        return {
            "source_type": "",
            "citation_or_derivation": raw.strip(),
            "scientific_meaning": "",
        }
    return {}


def normalize_comparison(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    return _COMPARISON_ALIASES.get(s, s)


def is_numeric_threshold_value(threshold: Any) -> bool:
    """True when threshold is a number or a pure numeric string (e.g. 0.05, '20%')."""
    if isinstance(threshold, bool) or threshold is None:
        return False
    if isinstance(threshold, (int, float)):
        return True
    if isinstance(threshold, str):
        s = threshold.strip()
        return bool(s) and bool(_PURE_NUMERIC.match(s))
    return False


def extract_qualitative_criterion(fs: dict[str, Any]) -> str:
    """Pull free-form falsification criterion for non-numeric comparisons."""
    for key in (
        "criterion",
        "qualitative_criterion",
        "falsification_criterion",
        "observable_criterion",
    ):
        val = fs.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    raw = fs.get("raw")
    if isinstance(raw, dict):
        for key in (
            "criterion",
            "qualitative_criterion",
            "falsification_criterion",
            "observable_criterion",
        ):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    thr = fs.get("threshold")
    if isinstance(thr, str) and thr.strip() and not is_numeric_threshold_value(thr):
        return thr.strip()
    return ""


def normalize_falsifier(fs: Any, *, label: str = "") -> dict[str, Any] | None:
    if not isinstance(fs, dict) or not fs:
        return None
    rationale = _as_rationale(
        fs.get("threshold_rationale")
        or fs.get("rationale")
        or fs.get("threshold_justification")
    )
    return {
        "label": label or str(fs.get("label") or fs.get("metric") or "?"),
        "metric": fs.get("metric"),
        "comparison": fs.get("comparison") or fs.get("op") or fs.get("operator"),
        "threshold": fs.get("threshold"),
        "dataset": fs.get("dataset"),
        "regime": fs.get("regime"),
        "criterion": (
            fs.get("criterion")
            or fs.get("qualitative_criterion")
            or fs.get("falsification_criterion")
        ),
        "threshold_rationale": rationale,
        "raw": fs,
    }


def _assess_rationale(
    rationale: dict[str, Any],
    *,
    threshold: Any,
) -> tuple[list[str], str]:
    """Validate threshold_rationale; return (issues, source_type)."""
    issues: list[str] = []
    source = str(rationale.get("source_type") or "").strip().lower()
    citation = str(
        rationale.get("citation_or_derivation")
        or rationale.get("citation")
        or rationale.get("derivation")
        or ""
    ).strip()
    meaning = str(
        rationale.get("scientific_meaning")
        or rationale.get("meaning")
        or ""
    ).strip()

    if not rationale:
        issues.append("缺少 threshold_rationale")
    else:
        if source not in _SOURCE_TYPES:
            issues.append(
                f"source_type 无效/缺失（需为 {sorted(_SOURCE_TYPES)} 之一）"
            )
        if len(citation) < 12:
            issues.append("citation_or_derivation 过短或缺失（须可追溯）")
        elif _VAGUE.search(citation) and len(citation) < 40:
            issues.append("citation_or_derivation 像空话/拍脑袋，缺少可追溯锚点")
        if len(meaning) < 12:
            issues.append("scientific_meaning 过短或缺失（须说明达标的科学含义）")
        elif _VAGUE.search(meaning) and len(meaning) < 30:
            issues.append("scientific_meaning 过空，未解释科学含义")

        if source == "literature" and not re.search(
            r"(claim_|doi:|10\.\d{4,}|arxiv|survey|论文|文献)",
            citation,
            flags=re.IGNORECASE,
        ):
            issues.append("literature 依据未含 DOI/claim_id/survey 等可追溯锚点")
        if source == "theory" and not (
            re.search(
                r"(推导|公式|定理|bound|deriv|不等式|模型)",
                citation,
                flags=re.IGNORECASE,
            )
            # 或者 citation 里就是一条真公式（E2E v12 实测：σ_thermal =
            # sqrt(k_B·T/N) 因为不含"推导/公式"这几个**字**被拒 —— 护栏要认
            # 结构，不要认词。判据：等式 + 数学函数/运算符。
            or (
                re.search(r"[\w)σδγε²³]\s*[=≈∝≤≥]", citation)
                and re.search(r"(sqrt|exp|log|ln|sum|prod|\^|/|·|×|√)", citation,
                              flags=re.IGNORECASE)
            )
        ):
            issues.append("theory 依据未见推导/公式要点")
        if source == "pilot" and not re.search(
            r"(n\s*=|样本|试跑|pilot|先导|预实验|观测)",
            citation,
            flags=re.IGNORECASE,
        ):
            issues.append("pilot 依据未见样本/先导观测描述")

    if _RATIO_HINT.search(str(threshold) or "") and not rationale:
        issues.append("比例/相对阈值缺少任何依据")

    return issues, source


def _assess_qualitative(fs: dict[str, Any], comparison: str) -> list[str]:
    issues: list[str] = []
    if not fs.get("metric"):
        issues.append("缺少 metric")
    criterion = extract_qualitative_criterion(fs)
    if len(criterion) < 8:
        issues.append(
            f"comparison={comparison} 须写清定性/存在性判据"
            "（criterion 字段，或 threshold 用文字描述可观察条件）"
        )
    elif _VAGUE.search(criterion) and len(criterion) < 30:
        issues.append("定性判据过空/模糊，须写清可机械观察的条件")
    return issues


def assess_one_threshold(fs: dict[str, Any]) -> dict[str, Any]:
    """Validate a single structured falsifier for threshold grounding.

    Branching:
    - Numeric threshold (or numeric comparison) → require grounded threshold_rationale.
    - comparison ∈ {qualitative, exists, not_exists} without a pure numeric
      threshold → require a clear qualitative/existence criterion only.
    """
    issues: list[str] = []
    label = str(fs.get("label") or "?")
    threshold = fs.get("threshold")
    comparison = normalize_comparison(fs.get("comparison"))
    rationale = _as_rationale(fs.get("threshold_rationale"))
    has_numeric_thr = is_numeric_threshold_value(threshold)
    is_non_numeric = comparison in _NON_NUMERIC_COMPARISONS
    source = ""

    if has_numeric_thr or (
        comparison in _NUMERIC_COMPARISONS and not is_non_numeric
    ):
        mode = "numeric"
        if not has_numeric_thr:
            if threshold is None or (
                isinstance(threshold, str) and not threshold.strip()
            ):
                issues.append(
                    "缺少数值 threshold"
                    "（若为定性/存在性证伪，请设 comparison=qualitative|exists|not_exists）"
                )
            else:
                issues.append(
                    f"threshold 非数值: {threshold!r}"
                    "（或改 comparison=qualitative|exists|not_exists）"
                )
        rat_issues, source = _assess_rationale(rationale, threshold=threshold)
        issues.extend(rat_issues)
    elif is_non_numeric:
        mode = comparison  # qualitative | exists | not_exists
        issues.extend(_assess_qualitative(fs, comparison))
    else:
        # Unknown / missing comparison, no pure numeric threshold
        mode = "numeric"
        hint = (
            "（若为定性/存在性证伪，请设 comparison=qualitative|exists|not_exists）"
        )
        if threshold is None or (isinstance(threshold, str) and not threshold.strip()):
            issues.append(f"缺少数值 threshold{hint}")
        elif isinstance(threshold, str) and not re.search(r"\d", threshold):
            issues.append(f"threshold 非数值: {threshold!r}{hint}")
        else:
            # Digits present but not a pure numeric value — ambiguous
            issues.append(
                f"threshold 无法解析为数值: {threshold!r}"
                f"；数值比较请用纯数字，定性请设 comparison=qualitative|exists{hint}"
            )
            rat_issues, source = _assess_rationale(rationale, threshold=threshold)
            issues.extend(rat_issues)

    return {
        "label": label,
        "threshold": threshold,
        "comparison": comparison or None,
        "mode": mode,
        "source_type": source or None,
        "passed": len(issues) == 0,
        "issues": issues,
        "looks_like_ratio": bool(_RATIO_HINT.search(str(threshold) or "")),
    }


def assess_threshold_grounding(
    falsifiers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate grounding check across final hypothesis falsifiers."""
    normalized: list[dict[str, Any]] = []
    for i, fs in enumerate(falsifiers, 1):
        item = normalize_falsifier(fs, label=str(fs.get("label") or f"H{i}"))
        if item:
            normalized.append(item)

    if not normalized:
        # 判决拆除批 3w（threshold_grounding.py:324 降格→H-OB1，经
        # output_validator._ADVISORY_CHECKS 生效）：本判据不再一票否决。
        # 且「解析失败」与「真无 falsifier」必须分开报——前者是格式问题
        # （写了却解析不出，别让 agent 去找不存在的『没写』），后者才是缺席。
        parse_failed = bool(falsifiers)
        if parse_failed:
            reason = (
                f"收到 {len(falsifiers)} 条 falsifier 输入但没有一条能解析成"
                "结构化形态（**解析失败，不是没写**）——检查每条是否带 "
                "label/threshold/comparison 等结构字段，而不是散文"
            )
        else:
            reason = "未声明任何 structured falsifier（预注册 head 里一条都没有）"
        return {
            "applicable": True,
            "passed": False,
            "parse_failed": parse_failed,
            "n_falsifiers": 0,
            "n_failed": 0,
            "failed_labels": [],
            "reason": reason,
            "per_falsifier": [],
        }

    per: list[dict[str, Any]] = []
    failed: list[str] = []
    n_numeric = 0
    n_qualitative = 0
    for item in normalized:
        row = assess_one_threshold(item)
        per.append(row)
        if row.get("mode") == "numeric":
            n_numeric += 1
        else:
            n_qualitative += 1
        if not row["passed"]:
            failed.append(row["label"])

    passed = not failed
    if passed:
        parts = []
        if n_numeric:
            parts.append(f"{n_numeric} 个数值 threshold 均有可追溯依据与科学含义")
        if n_qualitative:
            parts.append(
                f"{n_qualitative} 个定性/存在性证伪已写清可观察判据"
            )
        reason = "；".join(parts) if parts else "全部 falsifier 通过 threshold grounding"
    else:
        snippets = []
        failed_rows = [row for row in per if not row["passed"]]
        for row in failed_rows:
            snippets.append(
                f"{row['label']}（{row.get('comparison')}）: "
                + "; ".join(row["issues"][:2])
            )
        # 只说**这次真正触发的那一种**。
        #
        # 这里原来把三种失败模式的通用帮助文本一股脑倒出来，开头永远是"数值阈值
        # 禁止拍脑袋"。2026-08-19 实测：那个 run 一个数值阈值都没有（全定性），
        # 挂的是一条缺可观察判据的定性条 —— 人照着这句话去找"拍脑袋的数字"，
        # 找不到；模型也照着它改，改错方向。报错指向假原因比不报错更贵。
        numeric_failed = any(
            row.get("mode") != "qualitative" for row in failed_rows
        )
        qualitative_failed = any(
            row.get("mode") == "qualitative" for row in failed_rows
        )
        heads = []
        if numeric_failed:
            heads.append(
                "数值阈值缺依据：须补 literature / theory / pilot 等来源说明，"
                "或改用定性/存在性证伪"
            )
        if qualitative_failed:
            heads.append(
                "定性/存在性判据缺失：须写清**看到什么算不成立**"
                "（勿为此硬编伪精确数字）"
            )
        reason = "；".join(heads) + "——" + "；".join(snippets[:6]) + "。"

    return {
        "applicable": True,
        "passed": passed,
        "n_falsifiers": len(normalized),
        "n_numeric": n_numeric,
        "n_qualitative": n_qualitative,
        "n_failed": len(failed),
        "failed_labels": failed,
        "reason": reason,
        "per_falsifier": per,
    }


def collect_falsifiers_for_audit(state: State) -> list[dict[str, Any]]:
    """当前承诺的证伪判据。唯一来源：预注册 head（nodes.hypothesis.committed）。"""
    return committed_falsifiers(state)


async def _audit_threshold_grounding(
    state: State,
    falsifiers: list[dict[str, Any]] | None = None,
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    # 权威对象是**已提交的承诺集**（create_claim / prereg），不是调用方递来的
    # 草稿。E2E v12 实测：agent 把带完整 rationale 的 falsifiers 直接传给本
    # 工具 → 报告 passed=True；validator 收尾时按实际提交现算 → fail。两条
    # 路径审两份不同的数据，agent 拿着 pass 却被判 fail，无从理解。
    # 现在：有已提交承诺就审它（显式传参只作预览，报告里如实分列）；
    # 一条承诺都还没有时才审草稿，并明确标注 freeze 门会按提交重审。
    collected = collect_falsifiers_for_audit(state)
    supplied = list(falsifiers) if falsifiers else None
    draft_preview: dict[str, Any] | None = None
    if supplied and collected:
        items = collected
        draft_preview = assess_threshold_grounding(supplied)
    elif supplied:
        items = supplied
    else:
        items = collected
    report = assess_threshold_grounding(items)
    audited_source = (
        "committed" if (collected and items is collected)
        else "draft_preview"
    )

    artifact_id: str | None = None
    if save_report:
        body = {"n_input": len(items), **report}
        lines = [
            "# Threshold Grounding Audit",
            "",
            f"- **passed**: {report['passed']}",
            f"- n_falsifiers: {report['n_falsifiers']}",
            f"- n_numeric: {report.get('n_numeric', 0)}",
            f"- n_qualitative: {report.get('n_qualitative', 0)}",
            f"- n_failed: {report['n_failed']}",
            "",
            report["reason"],
            "",
            "## Per falsifier",
        ]
        for row in report["per_falsifier"]:
            mark = "✓" if row["passed"] else "✗"
            extra = ""
            if row["issues"]:
                extra = " — " + "; ".join(row["issues"])
            lines.append(
                f"- {mark} **{row['label']}** "
                f"(mode={row.get('mode')}, comparison={row.get('comparison')!r}, "
                f"threshold={row.get('threshold')!r}, source={row.get('source_type')})"
                f"{extra}"
            )
        content = (
            "\n".join(lines)
            + "\n\n```json\n"
            + json.dumps(body, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
        saved = save_hypothesis_singleton(
            state,
            "hypothesis_threshold_grounding",
            "Threshold_Grounding",
            content,
            metadata={
                "passed": report["passed"],
                "n_failed": report["n_failed"],
                "failed_labels": report["failed_labels"],
            },
        )
        artifact_id = saved["id"]

    state.hook_state["last_threshold_grounding"] = report

    out = {
        "status": "success",
        "passed": report["passed"],
        "artifact_id": artifact_id,
        "message": report["reason"],
        "audited_source": audited_source,
        **report,
    }
    if audited_source == "draft_preview":
        out["message"] += (
            "\n⚠️ 本次审的是你传入的**草稿**（还没有任何已提交承诺）。"
            "freeze 门与收尾校验会按 create_claim / prereg 的实际提交重审 —— "
            "把 rationale 写进提交本身，别只写在传参里。"
        )
    if draft_preview is not None and draft_preview.get("passed") and not report["passed"]:
        out["message"] += (
            "\n⛔ 分歧：你传入的草稿能通过，但**已提交的承诺集**没过（审计以"
            "提交为准）。修法不是再传一遍参数，而是用 create_claim / 重新保存"
            "未冻结 prereg，让提交本身带上完整 threshold_rationale。"
        )
        out["draft_preview_passed"] = True
    return out


register_tool(
    ToolDefinition(
        name="audit_threshold_grounding",
        description=(
            "审计假说 **threshold 依据**（数值）或 **定性/存在性判据**。\n\n"
            "- 数值比较（> < >= <= == !=）或纯数字 threshold："
            "须有 literature/theory/pilot 等 threshold_rationale + scientific_meaning；"
            "禁止拍脑袋比例。\n"
            "- 定性/存在性（comparison=qualitative|exists|not_exists）："
            "不要求编数字；须写清可观察判据（criterion 或 threshold 文字）。\n\n"
            "**Use when**：补全 falsifier 并 save pre_registration 草稿后、"
            "**freeze_artifact 之前**（必调）；validate 前再确认。\n"
            "passed=false → 补依据或改 comparison 分支并重 save 未冻结 prereg；"
            "禁止无依据数字 freeze；禁止为过门禁硬编伪精确阈值。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "falsifiers": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "[{metric, comparison, threshold?, threshold_rationale?, "
                        "criterion?, ...}]；"
                        "comparison 可为数值运算符或 qualitative|exists|not_exists；"
                        "留空则从预注册（prereg）里已冻结的命题声明提取"
                    ),
                },
                "save_report": {"type": "boolean", "default": True},
            },
        },
        allowed_node_types=["hypothesis"],
    ),
    _audit_threshold_grounding,
)
