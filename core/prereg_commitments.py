"""研究问题与闭合条件 —— 把冻结预注册里的承诺变成机械可查的账。

## 平台的科研公理（v0.5，2026-08-16 重构）

一个研究计划，不管什么学科什么范式，只欠四件事：

1. **说清要回答什么问题。** 至少一个，可以多个。
2. **开工前承诺"怎样算答完"。** 这份承诺冻结，不许事后改 —— 这是防
   HARKing（先看结果再编标准）的**一般形式**。
3. **关一个问题，必须逐项兑现当初的承诺，每项兑现挂得到真实证据。**
4. **"兑现得够不够好"是语义判断（归 reviewer）；"有没有兑现记录、
   证据指不指得到真东西"是机械判断（归框架，就是本模块）。**

这四条里**没有"假设"**。假设不是公理，是研究问题的一种形态。

## 核心对象：研究问题（ResearchQuestion）

    问题文本 · 产出形态 · 【可选】命题 · 闭合条件清单（冻结）

**写了命题的问题就是假设，不写就不是。** 不设 `是/否` 开关字段 ——
结构本身就是声明，没有第二个可以填错的地方，也没有"声明成 no 躲开判据"
这条绕行路径。

闭合条件两类，平台不认识别的分类，也不需要：

  - **数值条** `metric / comparison / threshold` —— 兑现记录来自
    experiment_log 的 `measured_metrics`：`status: measured`，或者这个量确实
    测不了时走预注册允许的**定性降级**（`status: estimated` + 公开申报
    `basis` 与 `degraded_reason`，见 `measurement_state`）。
  - **陈述条** `statement` —— 一句可观察的话（"成分 0–1 步长 0.1 全部扫完"、
    "列出 ≥3 条竞争解释且两两之间至少一件区分性证据"）。兑现记录来自
    `closure_discharges`，必须显式勾除并挂证据。

于是"测了某个量"只是闭合条件的一种。覆盖度达标、不确定度压进预算、
跟基线对照做完、竞争解释被证据区分、原文声明逐条对过、极限情形核对过 ——
全部是陈述条，**平台一个新分类都不用加**。

## 这次重构修的是什么

E2E-3 的论文写着 "H3 is refuted"，而 H3 判据里的 success_rate 从没测过。
为此加了"承诺账"：假设 → 一串必测的量 → 全测了才准翻判决。方向对，
但它把**假设**和**一串数字**焊死了：

  - 想写一条散文形式的假设 → 冻结闸拒绝（判据解析不出 metric）；
  - 想做探索/表征/复现/推导这类没有命题的研究 → 整套承诺机制**完全不生效**
    （`declares_hypotheses` 为假就全部跳过），少了平台最硬的一层保护；
  - 于是英国饮食文化那类课题为了过门，编出"差 30 年算削弱、差 20 年算证伪"。
    门要一个形式，模型就生产这个形式。

所以泛化的不是字母 H，是**账本的单位**：从"指标"泛化成"闭合条件项"。
防"没做也能说做完"那条红线原封不动，覆盖面反而从"有假设的研究"扩到了全部。

## 兼容

已冻结的历史协议改不了，`## Hypothesis N (Hx)` + ```yaml``` 判据三元组
**永远**读作"带命题的研究问题，编号 Hx，闭合条件全是数值条"。
解析层多认一种拼写而已，判据一字不变。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 兑现记录能挂在哪些工件上，是**类型属性**，声明在类型注册表里（那里同时声明
# 引用诚信、writing 输入门认不认）。这里曾硬编码一份 `_RESULT_TYPES` 名单 ——
# 新增证据类型时漏改它，闭合账本就对整类证据静默失效：勾了账也不算数。
from shared.lib.artifact_policy import carries_discharge_ledger

# ── 两种拼写 ────────────────────────────────────────────────────────────────

#: legacy：`## Hypothesis 3 (H3): Cost Reduction with Quality Preservation`
_HYP_HEADER_RE = re.compile(
    r"^#{1,4}\s*Hypothesis\s*\d+\s*\(\s*([QH]\d+)\s*\)\s*[:：]?\s*(.*)$",
    re.MULTILINE | re.IGNORECASE)

#: v0.5：`## Research Questions` 段落标题（也接受中文与下划线写法）
_QUESTIONS_SECTION_TITLES = (
    "research questions", "research_questions",
    "研究问题", "inquiry contract", "inquiry_contract",
)

#: v0.5：`### Q1: 这个体系低温区有什么相行为?`（也接受 H1 编号，便于沿用旧习惯）
_QUESTION_HEADER_RE = re.compile(
    r"^#{2,5}\s*([QH]\d+)\s*(?:[:：\-—．.、]|\s)\s*(.*)$",
    re.MULTILINE | re.IGNORECASE)

_YAML_BLOCK_RE = re.compile(r"```(?:yaml|yml)?\s*\n(.*?)```", re.DOTALL)

MEASURED = "measured"
ESTIMATED = "estimated"
DISCHARGED = "discharged"
_ACCEPTED_MEASUREMENT = (MEASURED,)
_ACCEPTED_DISCHARGE = (DISCHARGED,)
_KNOWN_STATUSES = (MEASURED, ESTIMATED, "not_run", "not_applicable")
_KNOWN_DISCHARGE_STATUSES = (DISCHARGED, "failed", "not_run", "not_applicable")

#: 定性降级的兑现记录必须带的两个字段（任一别名即可）。
#:
#: `basis` 回答"这个数是从哪来的"，`degraded_reason` 回答"为什么测不了"。
#: 两个都是**存在性**判据 —— 框架只查"写了没有"，写得成不成立归 reviewer。
_BASIS_KEYS = ("basis", "evidence", "derivation", "source")
_DEGRADED_REASON_KEYS = ("degraded_reason", "why_not_measured", "reason",
                         "degradation_reason")


def _first_text(entry: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = str(entry.get(k) or "").strip()
        if v:
            return v
    return ""


def measurement_state(entry: dict | None) -> tuple[str, str]:
    """一条数值条兑现记录的判定 —— **唯一**实现，所有消费方都从这里取。

    返回 `(state, note)`，state ∈ {"measured", "degraded", "open"}。

    ## 为什么 `estimated` 不再一律不算（2026-08-21）

    旧规则是 `status == "measured"` 才算，`estimated` 一律不算，理由写在
    E2E-3 上：那次靠"估计的成功率"把一个**从没执行过**的干预写成了 refuted。

    但这条规则同时封死了预注册**明文允许**的一条合法路径：数据不可得时做
    定性降级。实测代价：终态永远卡在"还欠 2 条 estimated"，研究做完了也关不掉。

    真正的问题是判据选错了层：「这个估计站不站得住」是**语义判断**，正则和
    枚举答不了；框架能机械回答的是另一个问题 —— **降级是不是被公开申报了**。

    所以改成：`estimated` 且同时写清了「数从哪来」`basis` 与「为什么测不了」
    `degraded_reason` → 算一条**降级兑现**（degraded），不再挡住终态；但它在
    账本、brief、writing 的 material_gaps 里**始终以 degraded 呈现**，绝不
    等同于 measured。E2E-3 那种情形在新规则下要过，必须白纸黑字写下
    "这个干预没有执行"—— 那正是当时缺的东西，而且它一写下来，reviewer 和
    论文读者就都看得见了。

    光写 `estimated` 不申报依据/原因 → 仍然是 open，且报错会**点名缺哪个字段**。
    """
    entry = entry if isinstance(entry, dict) else {}
    status = str(entry.get("status") or "").strip().lower()
    if status in _ACCEPTED_MEASUREMENT:
        return MEASURED, MEASURED
    if status != ESTIMATED:
        return "open", status or "（未申报）"
    missing = []
    if not _first_text(entry, _BASIS_KEYS):
        missing.append(f"`{_BASIS_KEYS[0]}`（这个估计值是从哪来的）")
    if not _first_text(entry, _DEGRADED_REASON_KEYS):
        missing.append(f"`{_DEGRADED_REASON_KEYS[0]}`（为什么这个量测不了）")
    if missing:
        return "open", f"estimated（缺 {'、'.join(missing)}）"
    return "degraded", "estimated（已申报定性降级）"

NUMERIC = "numeric"
STATEMENT = "statement"


def _clean(v: Any) -> str:
    return str(v).strip().strip("\"'").strip()


def _field_line(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"^\s*[-*+]?\s*\**\s*{name}\s*\**\s*[:：]\s*(.+?)\s*$",
        re.MULTILINE | re.IGNORECASE)


_METRIC_LINE = re.compile(r"^\s*-?\s*metric\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_STATEMENT_LINE = re.compile(
    r"^\s*-?\s*(?:statement|criterion|判据|条件)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_COMPARISON_LINE = re.compile(r"^\s*-?\s*comparison\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_THRESHOLD_LINE = re.compile(r"^\s*-?\s*threshold\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_ITEM_ID_LINE = re.compile(r"^\s*-?\s*id\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
#: `threshold_rationale:` 起一个嵌套块，其下的三个子字段缩进跟随。
_RATIONALE_HEAD = re.compile(
    r"^\s*-?\s*(?:threshold_)?rationale\s*[:：]\s*(.*)$", re.IGNORECASE)
_RATIONALE_FIELD = re.compile(
    r"^\s+(source_type|citation_or_derivation|citation|derivation|scientific_meaning|meaning)"
    r"\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)

_PROPOSITION_RE = _field_line("proposition")
#: `- assumption: 负面评价确实形成并固化过（survey 第 4 条提示它可能主要是被书写出来的）`
#: 可写多条。**所有研究问题都要有**，不是假设专属 —— 探索题同样有预设
#: （"扫这个参数区间"预设了答案在这个区间里）。
_ASSUMPTION_RE = re.compile(
    r"^\s*[-*+]?\s*\**\s*assumptions?\s*\**\s*[:：]\s*(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE)

#: 显式写出来的"没有命题"。**实测必须有这一层**（2026-08-16 英国饮食 A/B 跑）：
#: 模型不会把不适用的字段整行删掉，它会写
#:   `- **proposition**: （无 —— 这是一个时间定位问题，不是命题裁决）`
#: 而"字段非空 = 这是假设"会把两个纯解释型问题判成假设，
#: 把 falsifier / HIF / 阈值依据整套拖回来压在它们头上 —— 正是这次要消灭的东西。
#:
#: 这是个小名单，但**漏判的方向是安全的**：没认出来的写法一律当成"有命题"，
#: 也就是更严，不会 fail-open。
#
#: 三支分开写，是因为「空值词恰好是真命题的开头」这种 fail-open 真的会发生：
#:   "none of the baselines beat ours" 是一条命题，不是"没有命题"。
#:   "无关变量不影响收敛速度" 同理（无 后面直接跟词，不是空值）。
#: 所以拉丁词必须**整个值就是它**（或后接破折号/括号的说明），
#: 中文词必须后接非词字符（说明或结束）。
_NULL_PROPOSITION = re.compile(
    r"^\W*$"                                                  # 只有破折号 / 斜杠 / 空括号
    r"|^\W*(?:无|不适用|没有|不涉及|暂无)(?:\W|$)"                  # 无 —— 说明…
    r"|^\W*(?:none|n/?a|nil|null|not\s+applicable)\s*(?:[—–:：(（-]|$)",
    re.IGNORECASE)
_OUTPUT_KIND_RE = _field_line("output_kind")
_CRITERIA_HEADING = re.compile(
    r"(?:Falsification\s+Criteria|closure|闭合条件)", re.IGNORECASE)


# ── 数据模型 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClosureItem:
    """关掉一个研究问题需要兑现的一条。"""

    qid: str
    index: int
    kind: str                       # NUMERIC | STATEMENT
    item_id: str = ""
    metric: str | None = None
    comparison: str | None = None
    threshold: str | None = None
    statement: str | None = None
    #: 数值条的阈值依据（source_type / citation_or_derivation / scientific_meaning）。
    #: 实测事故（2026-08-17）：协议里写全了依据（literature + DOI + 科学含义），
    #: 解析时整个丢掉，阈值审计只收到 metric/comparison/threshold，于是永远判
    #: 「缺 threshold_rationale」—— 节点连试 12 次都过不去，最后 report_blocker。
    #: **解析器丢掉的字段，下游没有任何办法补回来。**
    rationale: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """兑现记录按什么键去查。

        数值条按 metric 名（与既有的 `measured_metrics` 约定一致，
        experiment 那边一个字都不用改）；陈述条按显式 id，没写就用
        `Q2#1` 这种位置键 —— 协议是冻结的，位置天然稳定。
        """
        if self.kind == NUMERIC and self.metric:
            return self.metric
        return self.item_id or f"{self.qid}#{self.index}"

    def describe(self) -> str:
        if self.kind == NUMERIC:
            tail = (f" {self.comparison} {self.threshold}"
                    if self.comparison else "")
            return f"`{self.metric}`{tail}"
        return f"`{self.key}` {self.statement or ''}".rstrip()


@dataclass(frozen=True)
class ResearchQuestion:
    qid: str
    title: str = ""
    output_kind: str = ""
    proposition: str = ""
    closure: tuple[ClosureItem, ...] = field(default_factory=tuple)
    assumptions: tuple[str, ...] = field(default_factory=tuple)
    legacy: bool = False

    @property
    def is_hypothesis(self) -> bool:
        """写了命题的问题就是假设 —— 不靠单独的开关字段。

        显式写成「无 / none / 不适用」的等同于没写（见 `_NULL_PROPOSITION`）：
        模型不会删整行，它会把"不适用"写进值里。
        """
        text = self.proposition.strip()
        return bool(text) and not _NULL_PROPOSITION.match(text)

    @property
    def metrics(self) -> list[dict[str, Any]]:
        """兼容视图：只看数值条，形状与 v3.8 的 `metrics` **逐字一致**。

        ⚠️ 不要往里加键。它有严格相等的调用方与测试（test_prereg_commitments
        那份 legacy 回放）。需要 rationale 的走 `parse_questions` 拿
        `ClosureItem.rationale`，别动这里。
        """
        return [
            {"metric": it.metric, "comparison": it.comparison,
             "threshold": it.threshold}
            for it in self.closure if it.kind == NUMERIC
        ]


# ── 闭合条件块解析 ──────────────────────────────────────────────────────────


def _parse_closure_block(block: str, qid: str, start_index: int) -> list[ClosureItem]:
    """一个判据块 → 闭合条件列表。**按记录分组的行扫描，不依赖 YAML 合法性。**

    为什么不用 yaml.safe_load：真实预注册的判据块里混着散文 measurement 段，
    形如 `- Treatment: run same tasks with routing:` 后跟缩进的 `*` 行 —— 不是
    合法 YAML。第一版 `safe_load` 抛异常后按"宁可不拦不可错拦"返回空，于是
    **H3 这条最重要的承诺被静默吞掉**（E2E-3 实测）。在最要紧的用例上静默降级，
    比不做还坏。

    分组规则：`metric:` 或 `statement:` 开一条新记录，其后的
    comparison / threshold / id 归属当前记录。`id:` 出现在记录第一行时先缓存，
    等下一条记录开出来再挂上去。
    """
    out: list[ClosureItem] = []
    cur: dict[str, Any] | None = None
    pending_id = ""
    index = start_index

    def _flush() -> None:
        nonlocal cur
        if cur is None:
            return
        out.append(ClosureItem(**cur))
        cur = None

    for line in block.splitlines():
        m = _METRIC_LINE.match(line)
        if m:
            val = _clean(m.group(1))
            # `metric: [a, b]` 是合取表头，真判据在 conditions 里逐条写
            if val.startswith("["):
                _flush()
                continue
            _flush()
            index += 1
            cur = {"qid": qid, "index": index, "kind": NUMERIC,
                   "item_id": pending_id, "metric": val,
                   "comparison": None, "threshold": None, "statement": None,
                   "rationale": {}}
            pending_id = ""
            continue

        s = _STATEMENT_LINE.match(line)
        if s:
            val = _clean(s.group(1))
            if not val:
                continue
            _flush()
            index += 1
            cur = {"qid": qid, "index": index, "kind": STATEMENT,
                   "item_id": pending_id, "metric": None,
                   "comparison": None, "threshold": None, "statement": val,
                   "rationale": {}}
            pending_id = ""
            continue

        i = _ITEM_ID_LINE.match(line)
        if i:
            val = _clean(i.group(1))
            if cur is not None and not cur.get("item_id"):
                cur["item_id"] = val
            else:
                pending_id = val
            continue

        if cur is not None:
            rh = _RATIONALE_HEAD.match(line)
            if rh:
                inline = _clean(rh.group(1))
                if inline:
                    cur["rationale"]["citation_or_derivation"] = inline
                continue
            rf = _RATIONALE_FIELD.match(line)
            if rf:
                k = rf.group(1).lower()
                k = {"citation": "citation_or_derivation",
                     "derivation": "citation_or_derivation",
                     "meaning": "scientific_meaning"}.get(k, k)
                cur["rationale"][k] = _clean(rf.group(2))
                continue

        if cur is None or cur["kind"] != NUMERIC:
            continue
        c = _COMPARISON_LINE.match(line)
        if c and cur["comparison"] is None:
            cur["comparison"] = _clean(c.group(1))
            continue
        th = _THRESHOLD_LINE.match(line)
        if th and cur["threshold"] is None:
            cur["threshold"] = _clean(th.group(1))

    _flush()
    return [it for it in out
            if (it.kind == NUMERIC and it.metric) or (it.kind == STATEMENT and it.statement)]


# ── 文档解析 ────────────────────────────────────────────────────────────────


def _questions_section(content: str) -> str | None:
    """取 `## Research Questions` 段正文（到下一个同级或更高级标题为止）。"""
    if not content:
        return None
    for match in re.finditer(r"^(#{1,4})\s*(.+?)\s*$", content, re.MULTILINE):
        title = match.group(2).strip().lower().strip("#*` ")
        if not any(t in title for t in _QUESTIONS_SECTION_TITLES):
            continue
        level = len(match.group(1))
        rest = content[match.end():]
        nxt = re.search(rf"^#{{1,{level}}}\s+\S", rest, re.MULTILINE)
        return rest[: nxt.start()] if nxt else rest
    return None


def _parse_modern(content: str) -> dict[str, ResearchQuestion]:
    body = _questions_section(content)
    if body is None:
        return {}
    heads = list(_QUESTION_HEADER_RE.finditer(body))
    out: dict[str, ResearchQuestion] = {}
    for i, head in enumerate(heads):
        qid = head.group(1).upper()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        block = body[head.end():end]
        prop = _PROPOSITION_RE.search(block)
        kind = _OUTPUT_KIND_RE.search(block)
        items: list[ClosureItem] = []
        for blk in _YAML_BLOCK_RE.findall(block):
            items.extend(_parse_closure_block(blk, qid, len(items)))
        out[qid] = ResearchQuestion(
            qid=qid,
            title=head.group(2).strip().strip("*` "),
            output_kind=_clean(kind.group(1)) if kind else "",
            proposition=_clean(prop.group(1)) if prop else "",
            closure=tuple(items),
            assumptions=tuple(
                _clean(m.group(1)) for m in _ASSUMPTION_RE.finditer(block)
                if _clean(m.group(1))
            ),
        )
    return out


def _parse_legacy(content: str) -> dict[str, ResearchQuestion]:
    """`## Hypothesis N (Hx)` + yaml 三元组 —— 永远读作带命题的研究问题。"""
    heads = list(_HYP_HEADER_RE.finditer(content or ""))
    out: dict[str, ResearchQuestion] = {}
    for i, head in enumerate(heads):
        qid = head.group(1).upper()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(content)
        section = content[head.end():end]
        items: list[ClosureItem] = []
        for blk in _YAML_BLOCK_RE.findall(section):
            items.extend(_parse_closure_block(blk, qid, len(items)))
        # 去重保序（同一 metric 在正文里可能被重复引用）
        seen: set[str] = set()
        uniq: list[ClosureItem] = []
        for it in items:
            if it.key in seen:
                continue
            seen.add(it.key)
            uniq.append(it)
        title = head.group(2).strip()
        out[qid] = ResearchQuestion(
            qid=qid, title=title,
            # legacy 文档里标题本身就是那条命题 —— 它天然是假设
            proposition=title or qid,
            closure=tuple(uniq), legacy=True,
            assumptions=tuple(
                _clean(m.group(1)) for m in _ASSUMPTION_RE.finditer(section)
                if _clean(m.group(1))
            ),
        )
    return out


def parse_questions(content: str) -> dict[str, ResearchQuestion]:
    """协议正文 → `{Q1: ResearchQuestion, ...}`。两种拼写都认，新格式优先。"""
    if not content:
        return {}
    modern = _parse_modern(content)
    legacy = _parse_legacy(content)
    if not modern:
        return legacy
    # 两种写法混用时以新格式为准，legacy 里独有的编号补进来（不丢账）
    merged = dict(legacy)
    merged.update(modern)
    return merged


def declares_questions(content: str) -> bool:
    """文档有没有用研究问题这套约定（任一拼写）。"""
    return bool(parse_questions(content))


def declares_hypotheses(content: str) -> bool:
    """文档里有没有**带命题**的研究问题（= 假设）。

    保留这个名字是因为它是既有调用方的入口；语义收窄为"有命题"，
    没有命题的研究（探索 / 表征 / 方法 / 解释 / 复现 / 推导）返回 False —— 它们
    不进 KB 的 hypothesis claim 通道，但**照样进闭合条件账**（见 frozen_questions）。
    """
    return any(q.is_hypothesis for q in parse_questions(content).values())


def closure_shape_hint() -> str:
    """闭合条件的合法写法 —— **唯一一份文案**。

    冻结闸有两个相邻分支（一条都没写 / 写了但解析不出），加上 freeze 前的审计，
    此前各写各的：只有其中一处提到"必须包在 ```yaml``` 块里"。模型第一次撞到的
    偏偏是没提的那条，于是它按字面改了字段名、再撞、再猜 —— 一个要求学了三轮，
    中间一轮还跑在自己编的假线索上（猜是 `criterion:` 不被认，而解析器其实认）。

    合法形状是**一件事**，不该有第二种说法。要改措辞就改这里。
    """
    return (
        "闭合条件必须写在 ```yaml``` 代码块里，每条以 `- metric:` 或 "
        "`- statement:` 起头：\n"
        "  数值条 `- metric: x` / `comparison: '>'` / `threshold: 1.2`"
        "（数值条还要 `threshold_rationale`）\n"
        "  陈述条 `- statement: \"成分 0–1 步长 0.1 全部扫完\"`\n"
        "两类地位平等 —— 写不出有依据的数字就写陈述条，"
        "**不要为了过门编一个精确到小数点的阈值**。"
    )


def sections_without_parsable_criteria(content: str) -> list[str]:
    """声称有闭合条件（或 Falsification Criteria）、却一条都解析不出来的问题段。

    冻结门禁用它 fail-loud —— 承诺解析不出来 = 这条问题根本没被对象化，
    后面的判定门禁对它形同虚设。宁可拒绝冻结，也不要留一个看不见的洞。
    """
    bad: list[str] = []
    questions = parse_questions(content)
    if not questions:
        return bad
    # 只对"声称写了判据"的段落 fail-loud，避免误伤只写了问题描述的草稿
    for qid, q in questions.items():
        if q.closure:
            continue
        section = section_text_for(content, qid)
        if section and _CRITERIA_HEADING.search(section):
            bad.append(qid)
    return sorted(bad)


def questions_without_closure(content: str) -> list[str]:
    """一条闭合条件都没有的研究问题 —— 冻结闸拒绝。

    "承诺怎样算答完"是四条公理里的第二条。没有它，这个问题永远关不掉，
    也永远没人能说它被糊弄了。
    """
    return sorted(qid for qid, q in parse_questions(content).items() if not q.closure)


def section_text_for(content: str, qid: str) -> str:
    """取某个问题段的正文（两种拼写都找）。"""
    for pattern in (_QUESTION_HEADER_RE, _HYP_HEADER_RE):
        heads = list(pattern.finditer(content or ""))
        for i, head in enumerate(heads):
            if head.group(1).upper() != qid.upper():
                continue
            end = heads[i + 1].start() if i + 1 < len(heads) else len(content)
            return content[head.end():end]
    return ""


def parse_commitments(content: str) -> dict[str, dict]:
    """兼容视图：`{H1: {"title":..., "metrics":[...]}}`，只含数值条。

    v3.8 的调用方（obligations / experiment sediment / threshold 审计）继续用它。
    没有数值条的问题不出现在这里 —— 与旧行为一致。
    """
    out: dict[str, dict] = {}
    for qid, q in parse_questions(content).items():
        metrics = q.metrics
        if metrics:
            out[qid] = {"title": q.title, "metrics": metrics}
    return out


# ── 找冻结的预注册 ──────────────────────────────────────────────────────────

def _iter_project_prereg_records(state: Any):
    """本工作区里全部 pre_registration（跨节点读，走账本）。"""
    seen: set[str] = set()
    try:
        for a in state.list_artifacts() or []:
            if a.get("type") != "pre_registration":
                continue
            rec = state.read_artifact(a.get("id"))
            if isinstance(rec, dict):
                seen.add(a.get("id") or "")
                yield rec
    except Exception:
        pass



def frozen_questions(state: Any) -> dict[str, ResearchQuestion]:
    """本项目预注册里的全部研究问题（含没有命题的）。

    ⚠️ 这里**不**再过滤 `metadata.frozen`，与 v3.8 的 `frozen_commitments` 行为
    逐字一致：产物在冻结前后是同一份身份。加过滤会让"冻结那一刻之前"的门禁失效，
    也会打断既有调用方（实测：会让 E2E-3 那条 refuted 重新一路绿灯）。
    """
    # ── 版本原语（RFC 2026-08-18）：一个身份只算**最新版**的承诺 ────────────
    #
    # 修订（amend）产生同一身份的新版本；旧版的承诺已被公开披露的差异记录取代，
    # 再把它们并进来就是 #414 的镜像 —— 已修订掉的 metric 永远关不掉，项目被
    # 一条不存在的承诺锁死。分组键 = (type, name)（即身份 stem）；同身份取
    # version 最大的记录。存量记录没有 version 字段 → 按 1 算：单版本身份行为
    # 与旧实现逐字一致（"不过滤 frozen"那条注释的语义保持不变）。
    #
    # 历史审计（"当年 v1 承诺过什么"）不走这里 —— 走账本（core/ledger）与 git 历史。
    latest: dict[str, dict] = {}
    for rec in _iter_project_prereg_records(state):
        key = f"{rec.get('type')}::{rec.get('name')}"
        try:
            version = int(rec.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
        held = latest.get(key)
        try:
            held_version = int((held or {}).get("version") or 1)
        except (TypeError, ValueError):
            held_version = 1
        if held is None or version > held_version:
            latest[key] = rec
    merged: dict[str, ResearchQuestion] = {}
    for rec in latest.values():
        for qid, q in parse_questions(rec.get("content") or "").items():
            merged.setdefault(qid, q)
    return merged


def frozen_commitments(state: Any) -> dict[str, dict]:
    """兼容视图：只含有数值条的问题。没有预注册 / 没有结构化判据 → 空。"""
    return {qid: {"title": q.title, "metrics": q.metrics}
            for qid, q in frozen_questions(state).items() if q.metrics}


# ── 兑现记录 ────────────────────────────────────────────────────────────────

_MEASURE_KEYS = ("measured_metrics", "metric_measurements")
_DISCHARGE_KEYS = ("closure_discharges", "discharged_closure_items")


def _absorb_block(md: dict, keys: tuple[str, ...], out: dict[str, dict]) -> None:
    for key in keys:
        block = md.get(key)
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            if isinstance(spec, dict):
                entry = dict(spec)
            elif isinstance(spec, str):
                entry = {"status": spec}
            else:
                entry = {"status": str(spec)}
            entry["status"] = str(entry.get("status") or "").strip().lower()
            out.setdefault(str(name).strip(), entry)


def _scan_result_metadata(state: Any, keys: tuple[str, ...]) -> dict[str, dict]:
    """把所有兑现登记块并成一份账 —— **跳过已被合法否定的那些产物**。

    `_absorb_block` 用 setdefault（最早的赢）+ `list_artifacts()` 的 created_at 升序，
    于是一份早期 blocked 日志里的 8 条 `not_run` 会**永久占位**，哪怕同一个 run 后来
    用合法冻结的 supersession 否定了那份草稿、并在 canonical 日志里写了 8 条
    `discharged`（#901 真实 UI run 实测：节点自己的 active view 与 scientific audit
    都读到了新日志，core 这边仍返回 0/8 → 假 closure debt → Writing 被错误硬拦）。

    改成"后写覆盖先写"是不安全的：那等于允许任意一份较晚的、非 canonical 的产物改账。
    正解是把**被否定的那份整个排除**，其余仍是最早的赢。判定归
    `core.supersession` 一处回答，节点与 core 读同一份。
    """
    out: dict[str, dict] = {}
    from core.supersession import superseded_artifact_ids

    superseded = superseded_artifact_ids(state)
    try:
        for a in state.list_artifacts() or []:
            if not carries_discharge_ledger(a.get("type") or ""):
                continue
            if str(a.get("id") or "") in superseded:
                continue
            rec = state.read_artifact(a.get("id"))
            if isinstance(rec, dict) and isinstance(rec.get("metadata"), dict):
                _absorb_block(rec["metadata"], keys, out)
    except Exception:
        pass
    return out


def malformed_ledger_blocks(state: Any) -> list[dict[str, str]]:
    """兑现登记块写成了**非对象**（典型：list of key 字符串）的 result 产物。

    `_absorb_block` 只认 `dict`（`{key: {status, evidence}}`）；写成 list 的
    `closure_discharges`/`measured_metrics` 被**静默丢弃**，fulfilled 记不上，闭合
    条件永远显示"未申报"。模型看不出是格式问题，只会盲目补写不同形状 —— E2E v34
    实测：closure_discharges 写成 `["STD_MEASURED","SLOPE_FIT"]`，churn v5→v8、
    烧 44.8M tokens、run incomplete（[[project_e2e_v34_closure_format_churn]]）。

    返回 [{artifact, key, shape}]，供 commitment brief 把"格式错了、要对象"明确回给
    模型，让它一次改对。**全程吞异常**：这个诊断绝不能让 commitment brief（全节点
    每轮都读）崩 —— 失败就当没有畸形块。
    """
    ledger_keys = _MEASURE_KEYS + _DISCHARGE_KEYS
    seen: set[tuple[str, str]] = set()
    found: list[dict[str, str]] = []
    try:
        for a in state.list_artifacts() or []:
            if not carries_discharge_ledger(a.get("type") or ""):
                continue
            rec = state.read_artifact(a.get("id"))
            md = rec.get("metadata") if isinstance(rec, dict) else None
            if not isinstance(md, dict):
                continue
            name = str(a.get("name") or a.get("id") or "?")
            for key in ledger_keys:
                if key in md and not isinstance(md.get(key), dict):
                    tag = (name, key)
                    if tag in seen:
                        continue
                    seen.add(tag)
                    found.append(
                        {"artifact": name, "key": key,
                         "shape": type(md.get(key)).__name__}
                    )
    except Exception:
        return []
    return found


def declared_measurements(state: Any) -> dict[str, dict]:
    """数值条的兑现记录 —— experiment_log 的 `measured_metrics`。

    形如 `{"cost_reduction_pct": {"status": "measured", "value": 24.0}}`。
    """
    return _scan_result_metadata(state, _MEASURE_KEYS)


def declared_discharges(state: Any) -> dict[str, dict]:
    """陈述条的兑现记录 —— experiment_log 的 `closure_discharges`。

    形如 `{"Q2#1": {"status": "discharged", "evidence": "experiment_log__Sweep"}}`。
    `discharged` 必须挂 evidence —— 空口勾除等于没勾。
    """
    return _scan_result_metadata(state, _DISCHARGE_KEYS)


def unfulfilled_items(state: Any, question_id: str) -> list[dict]:
    """该问题承诺要兑现、但至今没有合格兑现记录的闭合条件。"""
    q = frozen_questions(state).get((question_id or "").upper())
    if not q:
        return []
    measurements = declared_measurements(state)
    discharges = declared_discharges(state)
    rows: list[dict] = []
    for item in q.closure:
        if item.kind == NUMERIC:
            got = measurements.get(item.key) or {}
            state_, note = measurement_state(got)
            if state_ != "open":
                continue
            rows.append({"kind": NUMERIC, "key": item.key, "metric": item.metric,
                         "comparison": item.comparison, "threshold": item.threshold,
                         "describe": item.describe(),
                         "declared_status": note})
        else:
            got = discharges.get(item.key) or {}
            status = got.get("status")
            evidence = str(got.get("evidence") or "").strip()
            if status in _ACCEPTED_DISCHARGE and evidence:
                continue
            reason = (got.get("status") or "（未申报）")
            if status in _ACCEPTED_DISCHARGE and not evidence:
                reason = "discharged 但没挂 evidence"
            rows.append({"kind": STATEMENT, "key": item.key,
                         "statement": item.statement, "describe": item.describe(),
                         "declared_status": reason})
    return rows


def unmeasured_metrics(state: Any, hypothesis_id: str) -> list[dict]:
    """兼容视图：只返回没兑现的**数值条**。"""
    return [r for r in unfulfilled_items(state, hypothesis_id) if r["kind"] == NUMERIC]


# ── 关闭门禁 ────────────────────────────────────────────────────────────────

def status_flip_block(state: Any, claim: dict, hypothesis_id: str | None,
                      new_status: str) -> str | None:
    """hypothesis claim 翻 validated/refuted 前的机械检查。返回错误文案或 None。

    只在**本项目确有冻结预注册且解析出了闭合条件**时生效 —— 没预注册的项目不受影响。
    系统节点（_curator 治理路径）也不受影响，由调用方判断。
    """
    if new_status not in ("validated", "refuted"):
        return None
    if (claim or {}).get("claim_type") != "hypothesis":
        return None
    questions = {qid: q for qid, q in frozen_questions(state).items() if q.closure}
    if not questions:
        return None

    available = ", ".join(sorted(questions))
    if not hypothesis_id:
        return (
            f"⛔ 本项目已冻结预注册（含 {len(questions)} 个带闭合条件的研究问题："
            f"{available}）。把一个 hypothesis claim 翻成 {new_status!r} 属于"
            "**关闭一条预注册承诺**，必须用 `hypothesis_id` 指名关的是哪一条。\n"
            "这不是形式要求：E2E-3 把一个从没测过 success_rate 的 H3 写成了 "
            "'refuted'，因为没人问过'你承诺要做的做了吗'。"
        )
    qid = hypothesis_id.upper()
    if qid not in questions:
        return (f"⛔ `hypothesis_id={hypothesis_id!r}` 不在冻结预注册里。"
                f"可用：{available}。预注册冻结后不可改 —— 若确实是新问题，"
                "它不属于本次预注册的承诺范围，只能停在 provisional。")

    missing = unfulfilled_items(state, qid)
    if not missing:
        return None

    q = questions[qid]
    lines = [
        f"⛔ 不能把 {qid} 翻成 {new_status!r}：它的闭合条件里有 "
        f"{len(missing)}/{len(q.closure)} 条**没有合格的兑现记录**。",
    ]
    for row in missing:
        lines.append(f"  - {row['describe']} → 当前申报：{row['declared_status']}")
    lines.append(
        "闭合条件是**合取**：任何一条没兑现，这个问题既不能证实也不能证伪。"
        "陈述条 `discharged` 必须挂 evidence —— 空口勾除等于没勾。"
    )
    lines.append(
        "正确做法三选一：\n"
        "  ① 去把缺的那几条真做出来，然后在 experiment_log 的 metadata 里写：\n"
        "     数值条 `measured_metrics: {\"<metric>\": {\"status\": \"measured\", \"value\": ...}}`\n"
        "     陈述条 `closure_discharges: {\"<id>\": {\"status\": \"discharged\", "
        "\"evidence\": \"<artifact id 或 run id>\"}}`\n"
        "  ② 这个量**测不了**（数据不可得、资产受限）→ 走预注册允许的定性降级，"
        "把降级**公开申报**出来：\n"
        f"     `measured_metrics: {{\"<metric>\": {{\"status\": \"{ESTIMATED}\", "
        f"\"value\": ..., \"{_BASIS_KEYS[0]}\": \"<这个数从哪来>\", "
        f"\"{_DEGRADED_REASON_KEYS[0]}\": \"<为什么测不了>\"}}}}`\n"
        "     两个字段都必须非空 —— 框架只查写没写（这是机械判据），写得成不成立"
        "由 reviewer 判。降级条目在账本、brief 与论文材料缺口里**始终标注为"
        "定性降级**，不得写成已测得。\n"
        "  ③ 既测不了也说不清为什么 → claim 停在 `provisional`，在 reasoning 里"
        "写清哪一条没兑现、为什么，并在产物里如实标注证据强度。"
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class ClosureTally:
    """冻结承诺的兑现清点 —— 按**闭合条目**计，不按研究问题计。

    这是"这项研究做到哪了"的唯一机械答案。谁需要它都从这里取：调度器的路由
    仪表盘、writing 的输入门。此前 `research_situation` 自己走了一遍同样的
    循环 —— 两份"怎么算兑现"的实现，分叉时两边都不报错。

    数值条 / 陈述条分开计，因为这个区分是范畴路由的机械信号（全陈述条的研究
    不该派计算实验）。
    """

    total: int
    #: 已兑现（**含**定性降级的那些 —— 它们不再欠账，但也不是测得的）
    fulfilled: int
    open_numeric: int
    open_statement: int
    #: 未兑现条目的可读描述，供下游如实呈现（不得写成已确证）
    open_items: tuple[str, ...] = ()
    #: 以定性降级（`estimated` + 申报了 basis/degraded_reason）兑现的条目数
    degraded: int = 0
    #: 降级条目的可读描述。**必须一路传到读者面前** —— 它们计入 fulfilled，
    #: 若不单独呈现，"数据不可得所以估了一个"就会在下游看起来和"测出来了"
    #: 一模一样，那就是把 E2E-3 那个洞换个地方重新挖开。
    degraded_items: tuple[str, ...] = ()

    @property
    def open_total(self) -> int:
        return self.open_numeric + self.open_statement

    @property
    def measured(self) -> int:
        """真正测得/勾除的条目数（不含降级）。"""
        return self.fulfilled - self.degraded

    @property
    def all_open_are_statements(self) -> bool:
        return self.open_total > 0 and self.open_numeric == 0


def closure_tally(state: Any) -> ClosureTally | None:
    """清点冻结承诺的兑现情况。没有带闭合条件的冻结预注册 → None。

    None **不是"零兑现"**：它是"这项研究没有冻结承诺"，两者对下游的含义完全
    相反（前者是自由的探索/服务型工作，后者是欠着账）。调用方必须分开处理。
    """
    questions = {qid: q for qid, q in frozen_questions(state).items() if q.closure}
    if not questions:
        return None

    measurements = declared_measurements(state)
    discharges = declared_discharges(state)
    total = fulfilled = open_numeric = open_statement = degraded = 0
    open_items: list[str] = []
    degraded_items: list[str] = []

    for qid in sorted(questions):
        for item in questions[qid].closure:
            total += 1
            if item.kind == NUMERIC:
                state_, status = measurement_state(measurements.get(item.key))
                ok = state_ != "open"
                if state_ == "degraded":
                    degraded += 1
                    degraded_items.append(f"{item.describe()} —— {status}")
            else:
                got = discharges.get(item.key) or {}
                status = got.get("status") or "未申报"
                # 与 render_commitment_brief 同一条判据：空口勾除等于没勾。
                ok = status == DISCHARGED and bool(str(got.get("evidence") or "").strip())
                if status == DISCHARGED and not ok:
                    status = "discharged（缺 evidence）"
            if ok:
                fulfilled += 1
                continue
            open_items.append(f"{item.describe()} —— {status}")
            if item.kind == NUMERIC:
                open_numeric += 1
            else:
                open_statement += 1

    return ClosureTally(
        total=total,
        fulfilled=fulfilled,
        open_numeric=open_numeric,
        open_statement=open_statement,
        open_items=tuple(open_items),
        degraded=degraded,
        degraded_items=tuple(degraded_items),
    )


def render_commitment_brief(state: Any) -> str | None:
    """给节点看的"你欠了哪些账"段。没有冻结预注册就返回 None。"""
    questions = {qid: q for qid, q in frozen_questions(state).items() if q.closure}
    if not questions:
        return None
    measurements = declared_measurements(state)
    discharges = declared_discharges(state)
    lines = ["## 📌 预注册承诺账（机器生成 —— 冻结后不可改）"]
    for qid in sorted(questions):
        q = questions[qid]
        tag = "假设" if q.is_hypothesis else (q.output_kind or "研究问题")
        head = f"**{qid}**（{tag}）"
        if q.title:
            head += f"：{q.title}"
        lines.append(head)
        for item in q.closure:
            mark = "⬜"
            if item.kind == NUMERIC:
                state_, st = measurement_state(measurements.get(item.key))
                # 降级兑现单独一个记号：它不欠账了，但也**不是**测得的。
                # 用 ✅ 表示它就是把降级洗成测量，那正是要防的那件事。
                mark = {"measured": "✅", "degraded": "🔻"}.get(state_, "⬜")
            else:
                got = discharges.get(item.key) or {}
                st = got.get("status") or "未申报"
                ok = st == DISCHARGED and bool(str(got.get("evidence") or "").strip())
                if st == DISCHARGED and not ok:
                    st = "discharged（缺 evidence）"
                mark = "✅" if ok else "⬜"
            lines.append(f"  {mark} {item.describe()} —— {st}")
    lines.append(
        "闭合条件是**合取**：一个问题的所有条目都兑现之后，才能给它下终态"
        "（带命题的问题走 `update_claim_status` 翻 validated/refuted，需传 "
        "`hypothesis_id`，框架会机械核对）。兑现状态写在 experiment_log 的 "
        f"metadata 里：数值条 `measured_metrics`（取值 {'/'.join(_KNOWN_STATUSES)}）、"
        f"陈述条 `closure_discharges`（取值 {'/'.join(_KNOWN_DISCHARGE_STATUSES)}，"
        "discharged 必须挂 evidence）。"
    )
    lines.append(
        f"🔻 = **定性降级兑现**：这个量测不了时，预注册允许降级，但降级必须"
        f"公开申报 —— `{{\"status\": \"{ESTIMATED}\", \"value\": ..., "
        f"\"{_BASIS_KEYS[0]}\": \"<这个数从哪来>\", "
        f"\"{_DEGRADED_REASON_KEYS[0]}\": \"<为什么测不了>\"}}`，两个字段都非空。"
        f"框架只查写没写；写得成不成立由 reviewer 判。降级条目不挡终态，但在"
        f"论文与账本里**必须标注为定性降级，不得写成已测得**。"
        f"只写 `{ESTIMATED}` 不申报依据/原因的，仍然算欠账。"
    )
    # 畸形格式的兑现登记要**明确**回给模型 —— 否则它看到上面一片"未申报"，却不知道
    # 是自己写成了 list 被静默丢弃，只会盲目 churn 补写（E2E v34：v5→v8 烧 44.8M）。
    try:
        malformed = malformed_ledger_blocks(state)
    except Exception:
        malformed = []
    if malformed:
        problems = "；".join(
            f"`{m['key']}` 在 `{m['artifact']}` 里写成了 {m['shape']}（应为 dict）"
            for m in malformed
        )
        lines.append(
            f"⛔ **兑现登记格式错误，这几条没被记入**：{problems}。"
            "框架只认**对象格式**：`closure_discharges`/`measured_metrics` 必须是 "
            "`{\"<条目key>\": {\"status\": ..., \"evidence\": ...}}` 这样的 dict，"
            "**不是** key 字符串的 list。写成 list 的条目被静默丢弃、fulfilled 记不上、"
            "上面仍显示未申报 —— 这就是你反复补写却始终不闭合的原因。"
            "请把它改成对象格式（每条带 status 和真实 evidence 的 artifact_id/run_id）重发，"
            "一次改对，不要再换别的形状试。"
        )
    return "\n".join(lines)
