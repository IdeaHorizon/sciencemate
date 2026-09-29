"""scan_artifact_disagreements —— Phase H 工具。

设计意图（用户决策）：不加 challenge_kb_claim 硬工具，让 LLM 在 artifact 里**自由
写** "I disagree with claim_X because Y"。curator Mode 1 整合 artifact 时主动扫
这些模式，解析出 (claim_id, reason)，自动 propose `kb_claim_status_review`。

这是**少数派意见的反向通道** —— 不靠 LLM 调专门工具（容易忘 / 上下文不对），靠
artifact 自然语言表达 + curator 反向扫。

支持的模式（regex）：
  - "I disagree with claim_<id>"
  - "claim_<id> is wrong"
  - "claim_<id> may not apply to <scope>"
  - "challenge claim_<id>"
  - "我不同意 claim_<id>"
  - "claim_<id> 可能不适用"
"""
from __future__ import annotations

import re
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool
from shared.lib.artifact_text import artifact_text


# 各种 disagreement 表达式（捕获 claim_id + 后续 reason 短语）
# claim_id 格式：claim_<8 hex>
_CLAIM_ID_PATTERN = r"claim_[a-f0-9]{8,}"

# 表达式 → (regex, label)。regex 必须含 `(claim_<id>)` 捕获组
_DISAGREEMENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(
        rf"(?i)\b(?:I\s+disagree\s+with|我不同意)\s+({_CLAIM_ID_PATTERN})"
        rf"\s*(?:because|since|因为)?\s*([^\n.]{{0,400}})",
        re.IGNORECASE,
    ), "disagree"),
    (re.compile(
        rf"(?i)({_CLAIM_ID_PATTERN})\s+(?:is\s+wrong|是错的|过时|outdated)"
        rf"\s*(?:because|since|因为)?\s*([^\n.]{{0,400}})",
        re.IGNORECASE,
    ), "claim_is_wrong"),
    (re.compile(
        rf"(?i)({_CLAIM_ID_PATTERN})\s+(?:may\s+not\s+apply|可能不适用|"
        rf"doesn't\s+apply|不适用)\s*(?:to|on|于)?\s*([^\n.]{{0,400}})",
        re.IGNORECASE,
    ), "scope_mismatch"),
    (re.compile(
        rf"(?i)\bchallenge\s+({_CLAIM_ID_PATTERN})\s*[:：-]?\s*([^\n.]{{0,400}})",
        re.IGNORECASE,
    ), "challenge"),
]


def _scan_text_for_disagreements(text: str) -> list[dict]:
    """返 [{claim_id, label, reason, raw_span}]，去重相同 claim_id+label。"""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for pat, label in _DISAGREEMENT_PATTERNS:
        for m in pat.finditer(text):
            cid = m.group(1)
            reason = (m.group(2) if m.lastindex and m.lastindex >= 2
                      else "").strip().strip("：:-,. \"'")
            key = (cid, label)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "claim_id": cid,
                "label": label,
                "reason": reason[:400],
                "raw_span": m.group(0)[:500],
            })
    return out


async def _scan_artifact_disagreements(
    state: State,
    artifact_ids: list[str] | None = None,
    auto_propose: bool = True,
    **_: Any,
) -> dict:
    """扫 artifact 文本里 LLM 写的 "I disagree with claim_X" 等模式。

    用法（curator Mode 1 推荐）：
      ```
      scan_artifact_disagreements(artifact_ids=[<just produced ids>], auto_propose=True)
      ```
    返：{found: [{claim_id, label, reason, ...}], proposals_created: N}。
    `auto_propose=True` → 自动调 propose 写 kb_claim_status_review proposal 进 inbox。
    """
    artifacts = state.list_artifacts()
    if artifact_ids:
        artifacts = [a for a in artifacts if a["id"] in artifact_ids]

    all_findings: list[dict] = []
    for a in artifacts:
        rec = state.read_artifact(a["id"])
        if rec is None:
            continue
        # 结构化产物（content 是对象）照样要扫：里面一样可能写着
        # "I disagree with claim_x"。以前这里直接 finditer(dict) 当场炸掉，
        # 整个 curator 扫描连同已扫到的发现一起丢（shared/lib/artifact_text）。
        findings = _scan_text_for_disagreements(artifact_text(rec))
        for f in findings:
            f["source_artifact_id"] = a["id"]
            f["source_artifact_type"] = rec.get("type")
            all_findings.append(f)

    proposals_created = 0
    skipped_unknown = 0
    if auto_propose and all_findings:
        # 调 propose 工具创建 kb_claim_status_review proposal
        try:
            from core.tool_registry import execute as execute_tool
            # 同 claim_id 合并 reasons 一次 propose
            by_claim: dict[str, list[dict]] = {}
            for f in all_findings:
                by_claim.setdefault(f["claim_id"], []).append(f)
            for cid, items in by_claim.items():
                # 防 LLM 在 artifact 里乱写 claim id：先验证 KB 真有这条
                if state.get_kb_record("claims", cid) is None:
                    skipped_unknown += 1
                    continue
                summary_reason = "; ".join(
                    f"[{it['label']}] {it['reason'] or '(no reason given)'} "
                    f"(in {it['source_artifact_type']})"
                    for it in items[:5]
                )
                result = await execute_tool(
                    "propose",
                    state,
                    proposal_type="kb_claim_status_review",
                    target_entity="claims",
                    target_id=cid,
                    proposed_action="review",
                    reasoning=(
                        f"LLM 在 artifact 里写了 disagreement / challenge："
                        f"{summary_reason[:600]}"
                    ),
                    confidence=0.7,
                    extra={
                        "source_findings": items,
                        "auto_detected_by": "scan_artifact_disagreements",
                    },
                )
                if result.get("status") == "success":
                    proposals_created += 1
        except Exception as e:
            return {
                "status": "partial",
                "found": all_findings,
                "proposals_created": proposals_created,
                "propose_error": f"{type(e).__name__}: {e}",
            }

    return {
        "status": "success",
        "found": all_findings,
        "n_findings": len(all_findings),
        "proposals_created": proposals_created,
        "skipped_unknown_claim_ids": skipped_unknown,
        "scanned_artifacts": [a["id"] for a in artifacts],
    }


register_tool(
    ToolDefinition(
        name="scan_artifact_disagreements",
        description=(
            "扫 artifact 文本找 LLM 写的 disagreement / challenge KB claim 模式，"
            "可自动 propose kb_claim_status_review。\n\n"
            "**Use when**：curator Mode 1 整合 producing 节点 artifact 时——"
            "节点 owner 可能在自己 artifact 里写 'I disagree with claim_X because Y' "
            "（启发式表达不同意），这工具反向扫出来 + propose 给 user inbox。\n\n"
            "**支持模式**（中英）：\n"
            "  - I disagree with claim_xxx / 我不同意 claim_xxx\n"
            "  - claim_xxx is wrong / 是错的 / 过时\n"
            "  - claim_xxx may not apply / 可能不适用\n"
            "  - challenge claim_xxx\n\n"
            "**Use NOT when**：找 claim status 异常 → find_stale_validated /"
            " find_high_dispute_claims。本工具专找 LLM 主动 challenge 信号。\n\n"
            "**关键参数**：\n"
            "  - artifact_ids：要扫的 artifact id list（None=全扫，慎用）\n"
            "  - auto_propose：True 自动写 propose；False 仅返发现\n\n"
            "**返回**：found list + proposals_created 数"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要扫的 artifact id；None = 全 run 扫",
                },
                "auto_propose": {
                    "type": "boolean",
                    "default": True,
                },
            },
            "required": [],
        },
        risk_level="low",
    ),
    _scan_artifact_disagreements,
)
