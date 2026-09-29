"""研究原始输入的不可变锚（v3.7）—— L1 少翻译 + L2 不可变锚。

## 为什么

三轮 E2E 的构念漂移，主信道是**转述链**而不是恶意改写：

  用户原文 → orchestrator 重新措辞成 research_question → hypothesis 操作化

E2E-3 实证：用户给的 `V0 无法验证 ~ V3 可确定性验证`，prereg 里写成了
`V0 Deterministic ~ V3 Requires Human Judgment` —— **刻度整个反转**；
"低难度**且**可验证"（两个条件）被操作化成只测可验证性，**一个维度凭空消失**。
两处都发生在纯散文→散文的传递里，平台自己**完全无感**（是人几天后读 prereg
才发现的）。

## 设计

1. **逐字冻结**：项目第一条真实用户输入原样存盘，永不覆盖（append-only）。
   原文才 1-2KB，注入每个节点毫无压缩压力。
2. **谁都不能静默改 —— 包括 orchestrator 自己**。目标变更只能追加
   `amendment`（何时、原文、理由），原始 intake 永远保留。让漂移者当保管人
   等于没保管：E2E#1 的课题塌缩里 orchestrator 全程照单全收甚至推动。
3. **可见 ≠ 遵守**：注入原文不能保证不走样，但它让**核对成为可能** ——
   reviewer 从此有 diff 的对象，而此前它连对照物都没有。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("research_intake")

_FILENAME = "research_intake.json"
# 注入上限：原始输入通常 1-2KB；超长时截断并提示（原文仍完整存盘）
_INJECT_CHAR_CAP = 6000


def _path(project_root: Path | None) -> Path | None:
    return (project_root / _FILENAME) if project_root else None


def load_intake(project_root: Path | None) -> dict | None:
    p = _path(project_root)
    if p is None or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.debug("load_intake failed: %s", e)
        return None


def record_intake(project_root: Path | None, text: str, *,
                   source: str = "user", session_id: str = "") -> dict | None:
    """记录项目的原始研究输入。**已存在则不覆盖** —— 后续输入进 amendments。

    `session_id` = 这句话是在**哪个会话**里说的（#974）。这份记录是项目级的、
    会被逐字注入同项目的每一个会话，所以"谁在什么时候说的"必须跟着走：少了它，
    上一个会话里的一句「交给 Experiment 节点完成」在新会话里长得和当前用户的话
    一模一样，父编排器据此创建 child 并声称"用户明确点名"——而当前会话的消息
    全文里根本没有那几个字。

    返回当前 intake 记录（含 amendments）；project_root 为空时返 None。
    """
    p = _path(project_root)
    if p is None or not (text or "").strip():
        return None
    now = datetime.now(UTC).isoformat()
    existing = load_intake(project_root)
    if existing is None:
        rec = {
            "original_text": text.strip(),
            "recorded_at": now,
            "source": source,
            "session_id": str(session_id or ""),
            "amendments": [],
        }
    else:
        rec = existing
        # 与原文或最近一次修订完全相同 → 不是新指令（重启/重放），忽略
        prior = [rec.get("original_text", "")] + [
            a.get("text", "") for a in (rec.get("amendments") or [])
        ]
        if text.strip() in prior:
            return rec
        rec.setdefault("amendments", []).append({
            "text": text.strip(), "at": now, "source": source,
            "session_id": str(session_id or ""),
        })
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    except OSError as e:
        log.debug("record_intake write failed: %s", e)
    return rec


def _origin_label(entry_session: str, current_session: str) -> str:
    """这句话是**哪个会话**里说的 —— 相对读它的这一轮（#974）。

    三种，措辞刻意分开：
      · 本会话说的 → 当前这一轮的话；
      · 本项目**另一个会话**说的 → 是项目背景，不是这一轮的授权；
      · 不知道哪个会话（老记录 / CLI）→ 如实说不知道，**不冒充本会话的**。
    """
    if not current_session or not entry_session:
        return "来源会话不详"
    if entry_session == current_session:
        return "本会话"
    return "本项目的**另一个会话**"


#: 跨会话继承时必须跟着走的那句话。它回答的是 #974 验收 2 和 3。
_CROSS_SESSION_CAUTION = (
    "⚠️ 上面标着「另一个会话」或「来源会话不详」的段落是**项目背景**，不是"
    "这一轮用户交代的事。尤其：那里面的内部路由指令（点名某个节点）、授权、"
    "批准和资源上限**不构成本轮授权** —— 本轮要做什么、交给谁，只能依据"
    "本会话用户这一轮说的话。若你的路由理由是「用户明确点名 X」，那句话必须"
    "出自本会话；否则请如实说这是你按任务性质自己判断的。"
)


def render_intake_section(project_root: Path | None,
                           current_session_id: str = "") -> str | None:
    """给所有节点 system prompt 的逐字原文段（L1：切断转述链）。

    注意用词：明确告诉节点**这是权威原文，orchestrator 的转述仅供参考**。
    三轮实测的漂移都发生在"节点只见到转述版"的那一步。

    2026-09-21（#974）：这份记录是**项目级**的，会逐字注入同项目的每一个会话。
    此前每一段都不带来源会话，于是上一个会话里的一句「交给 Experiment 节点完成」
    在新会话里读起来和当前用户的话一模一样 —— 父编排器据此创建 Experiment child，
    并写道"用户明确点名「交给 Experiment 节点完成」"，而当前会话的消息全文里
    根本没有那几个字。所以每一段都标来源，且跨会话那些明确**不构成本轮授权**。
    """
    rec = load_intake(project_root)
    if not rec:
        return None
    original = rec.get("original_text") or ""
    if not original.strip():
        return None
    truncated = ""
    if len(original) > _INJECT_CHAR_CAP:
        original = original[:_INJECT_CHAR_CAP]
        truncated = f"\n…（原文过长已截断；完整原文见 {_FILENAME}）"
    cur = str(current_session_id or "")
    origin = _origin_label(str(rec.get("session_id") or ""), cur)
    cross_session = False
    if origin != "本会话":
        cross_session = True
    lines = [
        "## 📌 用户原始研究输入（逐字，权威）",
        "以下是用户对本项目的原话。**这是研究目标的唯一权威表述** —— "
        "node_inputs 里的 research_question 等字段是 orchestrator 的转述，"
        "**仅供参考；与本段冲突时以本段为准**。",
        "",
        f"### 项目原始输入（{rec.get('recorded_at', '')[:19]}，{origin}）",
        "```",
        original + truncated,
        "```",
    ]
    for i, a in enumerate(rec.get("amendments") or [], 1):
        # 来源如实标注：决策附言（HITL 打回时写的那段话）与聊天里的后续指令
        # 同为用户的话、同级权威，但 reviewer 核验出处时要分得清是哪一条通道。
        kind = ("用户决策附言" if a.get("source") == "decision_note"
                else "用户后续指令")
        a_origin = _origin_label(str(a.get("session_id") or ""), cur)
        if a_origin != "本会话":
            cross_session = True
        lines += [
            "",
            f"### {kind} #{i}（{a.get('at', '')[:19]}，{a_origin}）",
            "```",
            (a.get("text") or "")[:_INJECT_CHAR_CAP],
            "```",
        ]
    if cross_session and cur:
        lines += ["", _CROSS_SESSION_CAUTION]
    lines += [
        "",
        "**纪律**：定义、刻度方向、判据、限定条件必须与原文一致。若你的操作化"
        "与原文有出入（例如把两个条件简化成一个、或把等级方向调转），必须在产出里"
        "**显式说明这是有意的设计选择及理由**，不得静默改写。",
    ]
    return "\n".join(lines)
