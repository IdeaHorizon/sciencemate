"""audit_hypothesis_vs_conclusions — 检测假设是否复述论文结论或 validated claim。"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from .paper_reader import (
    extract_paper_audit_phrases,
    extract_paper_metric_tokens,
    get_cached_paper_texts,
)

_FINDING_SECTION_PATTERNS = (
    r"(?:关键\s*finding|key\s*findings?|主要结论|findings?|conclusions?)",
)
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for",
    "is", "are", "was", "were", "be", "than", "that", "this", "with",
    "的", "了", "在", "与", "和", "比", "更", "是", "有", "对", "为",
})


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _token_set(text: str) -> set[str]:
    return {
        t for t in _normalize(text).split()
        if len(t) > 2 and t not in _STOPWORDS
    }


def _overlap_ratio(a: str, b: str) -> float:
    ta, tb = _token_set(a), _token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _survey_headings(content: str) -> list[str]:
    """survey 正文里的全部标题（给"扫了什么、为什么一条都没抽到"用）。"""
    return [
        m.group(1).strip()
        for m in re.finditer(r"^#{1,6}\s+(.+)$", content, flags=re.MULTILINE)
    ]


def _list_items(lines: list[str], *, under_sections: bool | None) -> list[str]:
    """标题下的条目（`- …` / `1. …`）。under_sections=True 只取词表命中的段；
    None 取**全部**段。"""
    out: list[str] = []
    in_section = under_sections is None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        header_match = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if header_match:
            if under_sections is None:
                in_section = True
            else:
                in_section = bool(re.search(
                    "|".join(_FINDING_SECTION_PATTERNS), header_match.group(1),
                    flags=re.IGNORECASE,
                ))
            continue
        if in_section and (re.match(r"^[-*•]\s+", stripped) or re.match(r"^\d+\.\s+", stripped)):
            item = re.sub(r"^[-*•\d.]+\s*", "", stripped)
            item = re.sub(r"\*\*", "", item)
            if len(item) > 20:
                out.append(item)
    return out


def _extract_survey_findings(content: str) -> list[str]:
    """survey 里的 finding 条目。

    三级退化，每级都是"抽取器认不出结构"的兜底，不是"survey 没有内容"的证明：
      1. 词表命中的段（`Key findings` / `主要结论` …）下的条目；
      2. **全部**标题下的条目 —— survey 是同一批模型写的自由 markdown，标题跟着
         课题措辞走（实拍：`## 六、空白点列表（research gaps）`），生成端没有
         任何义务对齐这张词表（#760）；
      3. `finding: …` 行内写法。
    三级都空 → 返回空，由调用方按"没执行"处理，**不许**按"扫过了没问题"处理。
    """
    lines = content.splitlines()
    findings = _list_items(lines, under_sections=True)
    if not findings:
        findings = _list_items(lines, under_sections=None)
    if not findings:
        for m in re.finditer(
            r"(?:finding|结论|发现)[：:]\s*(.+?)(?:\n|$)", content, flags=re.IGNORECASE,
        ):
            findings.append(m.group(1).strip())
    return findings


def _collect_validated_claims(state: State, limit: int = 30) -> list[dict[str, Any]]:
    claims = state.list_kb("claims")
    out: list[dict[str, Any]] = []
    for rec in claims:
        if rec.get("status") not in ("validated", "open"):
            continue
        text = rec.get("claim_text") or rec.get("text") or ""
        if text:
            out.append({
                "id": rec.get("id", ""),
                "claim_text": text,
                "status": rec.get("status", ""),
            })
        if len(out) >= limit:
            break
    return out


def _audit_one(
    label: str,
    claim_text: str,
    findings: list[str],
    validated: list[dict[str, Any]],
    overlap_threshold: float,
    paper_metric_tokens: set[str] | None = None,
) -> dict[str, Any] | None:
    best_finding = ("", 0.0)
    for finding in findings:
        ratio = _overlap_ratio(claim_text, finding)
        if ratio > best_finding[1]:
            best_finding = (finding, ratio)

    best_claim = ("", "", 0.0)
    for vc in validated:
        ratio = _overlap_ratio(claim_text, vc["claim_text"])
        if ratio > best_claim[2]:
            best_claim = (vc["id"], vc["claim_text"], ratio)

    reasons: list[str] = []
    if best_finding[1] >= overlap_threshold:
        reasons.append(
            f"与 survey finding 词汇重叠 {best_finding[1]:.0%}："
            f"「{best_finding[0][:80]}…」"
        )
    if best_claim[2] >= overlap_threshold:
        reasons.append(
            f"与 KB {best_claim[0]} ({best_claim[2]:.0%} overlap)："
            f"「{best_claim[1][:80]}…」"
        )
    if paper_metric_tokens:
        claim_lower = claim_text.lower()
        matched = {t for t in paper_metric_tokens if t in claim_lower}
        if len(matched) >= 3:
            reasons.append(
                "假设包含 ≥3 个与参考论文摘要/结论相同的定量指标 "
                f"({', '.join(sorted(matched)[:6])})，疑似复述原文结论"
            )
    norm_claim = _normalize(claim_text)
    for finding in findings:
        nf = _normalize(finding)
        if len(nf) > 30 and (nf in norm_claim or norm_claim in nf):
            reasons.append("假设文本几乎包含 survey finding 原文")
            break

    if not reasons:
        return None
    return {
        "label": label,
        "claim_text": claim_text,
        "reason": "; ".join(reasons),
        "finding_overlap": round(best_finding[1], 3),
        "kb_overlap": round(best_claim[2], 3),
        "matched_claim_id": best_claim[0] or None,
    }


async def _audit_hypothesis_vs_conclusions(
    state: State,
    hypotheses: list[dict[str, Any]],
    survey_artifact_id: str = "",
    overlap_threshold: float = 0.55,
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    # hypotheses 非空由 parameters_schema 的 minItems=1 声明，派发口核取值。
    survey_rec: dict[str, Any] | None = None
    if survey_artifact_id:
        survey_rec = state.read_artifact(survey_artifact_id)
    else:
        for art in state.list_artifacts("survey_report"):
            survey_rec = state.read_artifact(art["id"])
            if survey_rec:
                survey_artifact_id = art["id"]
                break

    survey_text = str((survey_rec or {}).get("content") or "")
    findings: list[str] = _extract_survey_findings(survey_text) if survey_text.strip() else []
    if survey_text.strip() and not findings:
        # 缺席的检查不能长得跟通过了一样（#760 实拍：findings scanned: 0 →
        # passed=true → cleared=[Q1,Q2]，reviewer 抓到"结论复述检查从未真正执行，
        # 是空转通过"）。survey 非空而一条 finding 都抽不出 = 抽取器认不出这份
        # 文档，不是这份文档没有结论。如实报 not_executed，并列出扫到的标题，
        # 报告照存（证据可持久化），但**不发 passed**。
        headings = _survey_headings(survey_text)
        report_body = {
            "survey_artifact_id": survey_artifact_id or None,
            "not_executed": True,
            "reason": "survey 非空但抽不出任何 finding 条目 —— 抽取器认不出这份文档的结构",
            "scanned_headings": headings[:40],
            "n_findings_scanned": 0,
        }
        artifact_id: str | None = None
        if save_report:
            content = (
                "# Hypothesis vs Conclusion Audit\n\n"
                f"- survey: {survey_artifact_id or '(none)'}\n"
                "- **not_executed**: survey 非空但一条 finding 都没抽到（抽取器认不出结构）\n"
                "- scanned headings:\n"
                + "".join(f"  - {h}\n" for h in headings[:40])
                + "\n---\n\n```json\n"
                + json.dumps(report_body, indent=2, ensure_ascii=False)
                + "\n```\n"
            )
            art = state.save_artifact(
                "hypothesis_conclusion_audit", "Conclusion_Audit", content,
                metadata={"not_executed": True, "n_flagged": 0,
                          "survey_artifact_id": survey_artifact_id},
            )
            artifact_id = art["id"]
        return {
            "status": "not_executed",
            "report": report_body,
            "artifact_id": artifact_id,
            "message": (
                "⚠️ 结论复述检查**没有执行**：survey 非空但抽不出任何 finding 条目。"
                "这不是「没有复述」。把 survey 的关键发现整理成条目（`- …`）后重跑，"
                "或直接把 findings 写进 hypotheses 对照。"
            ),
        }

    for paper_text in get_cached_paper_texts(state).values():
        findings.extend(extract_paper_audit_phrases(paper_text))

    paper_metric_tokens: set[str] = set()
    for paper_text in get_cached_paper_texts(state).values():
        paper_metric_tokens |= extract_paper_metric_tokens(paper_text)

    validated = _collect_validated_claims(state)
    flagged: list[dict[str, Any]] = []
    cleared: list[str] = []

    for i, hyp in enumerate(hypotheses):
        label = hyp.get("label") or hyp.get("name") or f"H{i + 1}"
        claim_text = (hyp.get("claim_text") or hyp.get("hypothesis_text") or "").strip()
        if not claim_text:
            continue
        hit = _audit_one(
            label, claim_text, findings, validated, overlap_threshold, paper_metric_tokens,
        )
        if hit:
            flagged.append(hit)
        else:
            cleared.append(label)

    report_body = {
        "survey_artifact_id": survey_artifact_id or None,
        "n_findings_scanned": len(findings),
        "n_validated_claims_scanned": len(validated),
        "overlap_threshold": overlap_threshold,
        "flagged": flagged,
        "cleared": cleared,
        "passed": len(flagged) == 0,
    }

    artifact_id: str | None = None
    if save_report:
        md_lines = [
            "# Hypothesis vs Conclusion Audit",
            "",
            f"- survey: {survey_artifact_id or '(none)'}",
            f"- findings scanned: {len(findings)}",
            f"- validated claims scanned: {len(validated)}",
            f"- overlap threshold: {overlap_threshold}",
            f"- **passed**: {report_body['passed']}",
            "",
        ]
        if flagged:
            md_lines.append("## Flagged (疑似结论复述)")
            for item in flagged:
                md_lines.append(
                    f"- **{item['label']}**: {item['reason']}\n"
                    f"  claim: {item['claim_text'][:200]}"
                )
        else:
            md_lines.append("## Result\n全部候选假设未检测到结论复述。")

        content = (
            "\n".join(md_lines)
            + "\n\n---\n\n```json\n"
            + json.dumps(report_body, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
        art = state.save_artifact(
            "hypothesis_conclusion_audit",
            "Conclusion_Audit",
            content,
            metadata={
                "passed": report_body["passed"],
                "n_flagged": len(flagged),
                "survey_artifact_id": survey_artifact_id,
            },
        )
        artifact_id = art["id"]

    if flagged:
        msg = (
            f"⚠️ {len(flagged)} 条假设疑似复述论文结论/validated claim，"
            "请发散改写或提高预测非显然性后再 prereg。"
        )
    else:
        msg = f"✅ {len(cleared)} 条假设未检测到结论复述（threshold={overlap_threshold}）。"

    return {
        "status": "success",
        "passed": report_body["passed"],
        "flagged": flagged,
        "cleared": cleared,
        "report": report_body,
        "artifact_id": artifact_id,
        "message": msg,
    }


register_tool(
    ToolDefinition(
        name="audit_hypothesis_vs_conclusions",
        description=(
            "审核候选 hypothesis 是否复述 survey 论文结论、参考论文原文或 KB validated claim。\n\n"
            "**Use when**（hypothesis 节点必调，在 score_hypothesis_innovation 之前或之后）：\n"
            "  - 候选假设起草后，写 pre_registration 之前\n"
            "  - HIF 评分中 R≥3 或怀疑假设只是把 paper finding 改写成 hypothesis\n"
            "  - 单篇论文 survey 后思维过于围绕原文结论\n\n"
            "**Do NOT use when**：\n"
            "  - prereg 已 freeze\n"
            "  - 还没读 survey_report\n\n"
            "**返回**：flagged 列表 + hypothesis_conclusion_audit artifact。"
            "flagged 非空 → 必须重写或丢弃该假设，不可直接 create_claim。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "hypotheses": {
                    "type": "array",
                    "description": "待审核候选假设",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "claim_text": {"type": "string"},
                        },
                        "required": ["claim_text"],
                    },
                    "minItems": 1,
                },
                "survey_artifact_id": {
                    "type": "string",
                    "description": "survey_report artifact id；留空则自动找本 run 的 survey",
                },
                "overlap_threshold": {
                    "type": "number",
                    "description": "词汇重叠阈值 0–1，默认 0.55",
                    "default": 0.55,
                },
                "save_report": {
                    "type": "boolean",
                    "default": True,
                },
            },
            "required": ["hypotheses"],
        },
        allowed_node_types=["hypothesis"],
    ),
    _audit_hypothesis_vs_conclusions,
)
