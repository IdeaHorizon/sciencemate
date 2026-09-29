"""一次呈递 = 一个被冻结的 Offer；所有渲染都是它的投影，答复只能指回它里面的 choice。

## 为什么要有这个模块

在此之前，系统里**「一个选项」从来没有身份，只有文案**。同一次呈递的选项集在四个
地方各自构造，每一跳都拿文案当身份：

    _normalize_options()        → {label, description}                  无 id
    PauseEvent.options          → list[str]                             无 id
    平台 optionDetails 投影      → {label, description, recommended}      id 被丢掉
    前端 normalizeOption         → {id: value, label: value, value}       id = 文案本身
    答复                         → "PROCEED to next stage"                文案就是身份

2026-08-19 实测事故（英国饮食那趟，人点了三次 PROCEED，卡片原地重现三次）：

    decision_package.py:1131   options   = _build_option_labels(…)          ← 漏传 curator_pending
                                          → [1] "PROCEED to next stage"
    decision_package.py:984    actions   = _CURATOR_PENDING_ACTIONS
                                          → [1] "retry_curator"

同一次呈递、同一毫秒，**UI 拿到一个框架已经明确撤下的选项**。人点它 → 前端回传文案
→ `_parse_choice` 拿这段文案去匹配另一套动作名 → 认不出 → 静默 fail-closed → 账本
一个字不动 → 下游门禁继续拦 → 只能重新呈递同一张卡 → 无限循环。

四层没有任何一层能发现，因为四层比的都是文案。而更坏的静默形态是：人若答 "1"，
索引路径会把它解析成 `retry_curator` —— **屏幕上写着 PROCEED，账本记成重跑 curator**，
两边都不报错。

补那一行漏传的参数，等于把第五份抄件也对齐一次。根因是**不该有五份**。

## 不变量

一次呈递构造**一个** Offer，之后：

    offer.to_ascii_options()   → 决策包正文里人读的那段
    offer.to_pause_payload()   → PauseEvent / 平台 / UI 的结构化选项
    offer.choice_ids()         → 账本的合法动作集、解析器的判据

想让这三者分叉，得先把 `offer.choices` 拆成两个 —— 结构上做不到。这与
`ToolDefinition.content_contract`（PR#523）是同一个模式：一份声明，多个消费者。

## 身份分两层

`decision_id` 跨重呈递稳定（"这个决策"），`offer_id` 每次呈递都变（"这一次呈递"）。
于是：

  - 重呈递不会和旧答复混淆 —— 旧 offer_id 的答复被**明确拒绝**并指向新 offer，
    而不是静默丢失（2026-08-17 重呈递判死事故的反面：那次是 id 不变导致被判重复）。
  - 平台账本按 decision_id 归并同一个决策的多次呈递，UI 能如实显示"这是第 N 次问你"。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


#: pause 事件里承载「这一次呈递」的键。
#:
#: 中间各层（decision_package → PauseEvent → 平台 ingest → API → 前端）只搬运
#: 这**一个值**，不认识它的内部字段，也不许改写它的键名（连 snake_case 都原样
#: 留着 —— 它是被运输的外来对象，不是平台自己的数据）。
#:
#: 为什么必须是"一个不透明值"而不是"一组字段"：字段一旦在中途被按名字列举，
#: 那一处就成了会静默漏掉的地方。本模块设立时消灭了四份抄件，随后在两个边界
#: 上又各长出一份（decision_package 组 pause_event 时漏掉 `kind`，平台 ingest
#: 组 optionDetails 时漏掉 `id`/`offer_id`/`decision_id`）—— 两边都不报错，
#: 代价是人点三次没反应。搬运整个对象，才没有"漏掉"这个动作可做。
PAUSE_OFFER_KEY = "offer"


class OfferContractError(ValueError):
    """Offer 自己不合法 —— 构造期就炸，别让它流到 UI 上去。"""


@dataclass(frozen=True)
class Choice:
    """一个选项。`id` 是身份，`label` 只是给人看的文案。

    `id` 必须是稳定动作名（`proceed` / `retry_curator` / …）：它进账本、进平台
    Decision 行、进答复，改文案不该影响它。
    """

    id: str
    label: str
    description: str = ""

    def __post_init__(self) -> None:
        if not _STABLE_ID.match(self.id or ""):
            raise OfferContractError(
                f"choice id 必须是稳定动作名（小写字母/数字/下划线）：{self.id!r}"
            )
        if not (self.label or "").strip():
            raise OfferContractError(f"choice {self.id!r} 缺 label")


_STABLE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Offer:
    """一次呈递。冻结之后，任何渲染都只能从它派生。"""

    decision_id: str
    kind: str
    question: str
    choices: tuple[Choice, ...]
    context: str = ""
    recommended_id: str | None = None
    #: 呈递方想附带的事实（producing_run_id / review_failed / …）。只读地透传给
    #: 各消费方，不参与选项集判定 —— 判定只看 `choices`。
    facts: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.choices:
            raise OfferContractError("Offer 必须至少有一个 choice")
        ids = [c.id for c in self.choices]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise OfferContractError(f"choice id 重复：{sorted(dupes)}")
        if self.recommended_id is not None and self.recommended_id not in ids:
            raise OfferContractError(
                f"recommended_id={self.recommended_id!r} 不在选项集 {ids} 里"
            )

    # ── 身份 ──────────────────────────────────────────────────────────────

    @property
    def offer_id(self) -> str:
        """这一次呈递的身份 —— 由 decision_id + 选项集内容派生。

        选项集变了就是另一次呈递（curator 修好之后 `retry_curator` 换成
        `proceed`，那确实是另一个问题），因此对着旧菜单作出的答复必须被拒绝
        而不是被沿用。派生而非随机：同一个 offer 重算两次得到同一个 id，
        重放和幂等都不需要额外记账。
        """
        material = "|".join([self.decision_id, self.kind, *(c.id for c in self.choices)])
        return f"{self.decision_id}:o{hashlib.sha256(material.encode()).hexdigest()[:8]}"

    # ── 投影：三个消费者，一个来源 ─────────────────────────────────────────

    def choice_ids(self) -> tuple[str, ...]:
        """账本 / 解析器的合法集。"""
        return tuple(c.id for c in self.choices)

    def choice(self, choice_id: str) -> Choice | None:
        return next((c for c in self.choices if c.id == choice_id), None)

    def to_pause_payload(self) -> dict:
        """PauseEvent → 平台 → UI 的结构化选项。**带 id**，这是与旧格式的实质差别。

        同时给出 `options`（纯文案数组）作为老消费方的兼容层，但它是从同一份
        `choices` 派生的，不可能与结构化那份分叉。
        """
        details = [
            {
                "id": c.id,
                "label": c.label,
                "description": c.description,
                "recommended": c.id == self.recommended_id,
            }
            for c in self.choices
        ]
        return {
            "question": self.question,
            "context": self.context,
            "options": [c.label for c in self.choices],   # 兼容层，派生自同一来源
            "option_details": details,
            "offer_id": self.offer_id,
            "decision_id": self.decision_id,
            # `kind` 参与 offer_id 的派生，所以它必须随 payload 走 —— 否则收到
            # 这份 payload 的一方还原不出同一个 offer，只能"大致重建"，而
            # 大致重建正是这套东西要消灭的东西。
            "kind": self.kind,
            "recommended_option_index": self._recommended_index(),
            "recommended_choice_id": self.recommended_id,
            # 呈递方附带的事实（review_failed / retry 余额 / 需要几个人批 …）。
            #
            # 这个字段的注释一直写着"只读地透传给各消费方"，但它此前**不在这份
            # 投影里**，也没有任何消费方读它 —— 声明了一条管道，一步都没接。
            # 后果是人在面板上只看得见五个动作名，看不见"为什么问我"：
            # `review_failed=true` 这类判断依据全留在了呈递方进程里。
            #
            # 判定仍然只看 `choices`（facts 不参与 offer_id 派生），所以往里加
            # 字段不会让旧答复失效。
            "facts": dict(self.facts or {}),
        }

    @classmethod
    def from_pause_payload(cls, payload: dict) -> "Offer":
        """从 pause payload **还原**这一次呈递 —— 答复要对着它解析。

        为什么要还原而不是"照 payload 里的 id 列表判一下就完了"：判答复需要的
        不只是合法 id 集，还有**顺序**（裸序号要与屏幕同源取项）和 **label**
        （老前端回传文案时要精确匹配）。只带走一个 id 列表，就又回到了
        "两处各自持有半份选项集"的老路。

        还原后重算 `offer_id` 与传输来的那份比对：对不上说明这份 payload 在路上
        被改过或截断过。那时**宁可吵**，也不能拿一个半真的选项集去判人的授权 ——
        判错的代价是把人的决定悄悄换成另一个动作。
        """
        data = dict(payload or {})
        # 信封形态：中间层搬运的是 {PAUSE_OFFER_KEY: <这份 payload>}。两种形态都
        # 收，因为老 checkpoint 里存的是平铺的那份。
        carried = data.get(PAUSE_OFFER_KEY)
        if isinstance(carried, dict) and carried:
            data = dict(carried)
        details = data.get("option_details")
        if not isinstance(details, list) or not details:
            raise OfferContractError(
                "pause payload 里没有 option_details —— 还原不出这次呈递的选项集"
            )
        choices = tuple(
            Choice(
                id=str(d.get("id") or ""),
                label=str(d.get("label") or ""),
                description=str(d.get("description") or ""),
            )
            for d in details
            if isinstance(d, dict)
        )
        offer = cls(
            decision_id=str(data.get("decision_id") or ""),
            kind=str(data.get("kind") or ""),
            question=str(data.get("question") or ""),
            choices=choices,
            context=str(data.get("context") or ""),
            recommended_id=(str(data["recommended_choice_id"])
                            if data.get("recommended_choice_id") else None),
            facts=dict(data.get("facts") or {}),
        )
        transmitted = str(data.get("offer_id") or "")
        if transmitted and transmitted != offer.offer_id:
            raise OfferContractError(
                f"offer_id 对不上：payload 说 {transmitted!r}，"
                f"按它自己的 option_details 重算是 {offer.offer_id!r} —— "
                "这份呈递在传输中被改过，拒绝据此判定答复。"
            )
        return offer

    def to_ascii_options(self) -> str:
        """决策包正文里人读的那段选项区。"""
        lines = []
        for i, c in enumerate(self.choices, start=1):
            star = "  ← recommended" if c.id == self.recommended_id else ""
            lines.append(f"  [{i}] {c.label}{star}")
            if c.description:
                lines.append(f"      {c.description}")
        return "\n".join(lines)

    def _recommended_index(self) -> int | None:
        if self.recommended_id is None:
            return None
        return next(
            (i for i, c in enumerate(self.choices) if c.id == self.recommended_id), None
        )


# ── 答复 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Answer:
    """人的答复。指回**哪一次呈递的哪个选项** —— 两个 id 都在，缺一不可。"""

    offer_id: str
    choice_id: str
    note: str = ""


@dataclass(frozen=True)
class Rejection:
    """答复不合法。**必须吵**，且必须带合法出口。

    此前这里是 `return None` + 一行 transcript：授权凭空消失，人看不到任何反馈，
    只能再点一次，再消失一次。「契约必须送到调用方」—— 合法取值只在运行时报错
    等于逼人猜，而这里连报错都没有。
    """

    code: str
    message: str
    legal_choice_ids: tuple[str, ...]
    offer_id: str = ""

    def as_dict(self) -> dict:
        return {
            "status": "error",
            "code": self.code,
            "error": self.message,
            "legal_choice_ids": list(self.legal_choice_ids),
            "offer_id": self.offer_id,
        }


def resolve_answer(offer: Offer, raw: Any) -> Answer | Rejection:
    """把回传的东西解析成对 `offer` 的一个合法答复。

    **首选路径**是结构化答复 `{offer_id, choice_id}` —— 那时这个函数只做集合成员
    判断，不做任何猜测。

    自由文本 / 裸序号只在**兼容期**受理（CLI 里人手敲 "1"、老前端回传文案），且
    受理规则严格得多：

      - 序号按**本 offer 的选项顺序**取 —— 与渲染同源，不可能像以前那样
        「屏幕上是 PROCEED，索引取到 retry_curator」。
      - 文案必须**精确匹配**某个 choice 的 label 或 id，不再做子串匹配。子串匹配
        正是让 "PROCEED to next stage" 去撞 `proceed` 的那条路：它在选项集变化时
        会安静地匹配到另一个动作，或者安静地什么都匹配不上。

    认不出就返回 `Rejection`（带合法集），**绝不返回 None**。
    """
    legal = offer.choice_ids()

    if isinstance(raw, dict):
        offer_id = str(raw.get("offer_id") or "").strip()
        choice_id = str(raw.get("choice_id") or "").strip()
        note = str(raw.get("note") or "")
        if offer_id and offer_id != offer.offer_id:
            return Rejection(
                code="offer_superseded",
                message=(
                    "这条答复回答的是上一次呈递（选项集此后变了）。"
                    "请对**当前**这次呈递重新作答。"
                ),
                legal_choice_ids=legal,
                offer_id=offer.offer_id,
            )
        if choice_id:
            if offer.choice(choice_id) is None:
                return Rejection(
                    code="choice_not_offered",
                    message=f"{choice_id!r} 不在本次呈递的选项集里。",
                    legal_choice_ids=legal,
                    offer_id=offer.offer_id,
                )
            return Answer(offer_id=offer.offer_id, choice_id=choice_id, note=note)
        raw = str(raw.get("text") or "")

    text = str(raw or "").strip()
    if not text:
        return Rejection(
            code="empty_answer",
            message="没有收到任何选择。",
            legal_choice_ids=legal,
            offer_id=offer.offer_id,
        )

    # 兼容：裸序号（"1" / "[1]" / "1 因为…"）。与渲染同源取项。
    m = re.match(r"^\D{0,2}(\d+)\b", text)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(offer.choices):
            return Answer(
                offer_id=offer.offer_id,
                choice_id=offer.choices[idx].id,
                note=text[m.end():].strip(),
            )
        return Rejection(
            code="choice_index_out_of_range",
            message=f"本次呈递只有 {len(offer.choices)} 个选项，没有第 {idx + 1} 个。",
            legal_choice_ids=legal,
            offer_id=offer.offer_id,
        )

    # 兼容：精确匹配 label 或 id（大小写不敏感，去掉首尾空白）。不做子串匹配。
    folded = text.casefold()
    for c in offer.choices:
        if folded == c.id.casefold() or folded == c.label.strip().casefold():
            return Answer(offer_id=offer.offer_id, choice_id=c.id, note="")

    return Rejection(
        code="unrecognized_answer",
        message=(
            "认不出这是哪个选项。请回传 choice_id（或选项序号）。"
            f"本次呈递的合法选项：{', '.join(legal)}。"
        ),
        legal_choice_ids=legal,
        offer_id=offer.offer_id,
    )
