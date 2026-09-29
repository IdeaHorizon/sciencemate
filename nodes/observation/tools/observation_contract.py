"""observation 节点的产物契约 —— 写入门与冻结门。

## 这个节点防的原罪和 experiment 不是同一个

experiment 是**干预式**取证：让世界（或它的模型）发生一件本不会发生的事。它的
原罪是**伪造执行** —— 没跑说跑了、改了输出。所以它的闸围绕"这个数真是这次运行
产的"：可回放解析、输出 hash、执行记录。

observation 是**检视式**取证：世界已经把记录留下了，系统性地去看。它的原罪是
**摘樱桃** —— 只捡支持自己的材料。伪造执行那套闸在这里一条也拦不住摘樱桃：
每条引用都真实存在、每个 DOI 都解析得开，照样可以只挑对自己有利的那七条。

## 但纪律不能只有一套 —— 那会把这个节点做成"只会做系统性综述的节点"

第一版把"协议先冻再取证"当成了**所有** observation run 的硬闸。那默认了一件
事：**你开工前就知道自己在找什么**。

这对系统性综述 / meta-analysis 成立，对整类研究不成立 —— 扎根理论明确要求范畴
从材料里涌现而**不能**预设；史学考据常常是顺着线索走的；数据探索的价值恰恰在
撞见没预期的模式。硬要它们先冻协议，等于把发现型研究全判成不合规。

那正是 experiment 那个病的同款：experiment 的闸默认"研究 = 跑计算实验"，而
第一版 observation 的闸默认"研究 = 做系统性综述"。

## 正确的分法：两种模式，纪律不同但都硬

    exploratory（发现）   不要求冻协议 —— "什么算相关"本来就是这一趟要找的
                          ⛔ **机械禁止勾除闭合条目**
                          产物是 findings：新模式、证据张力、涌现的新问题

    confirmatory（兑现）  必须先冻协议，覆盖度必须有分母
                          ✅ 可以勾除闭合条目

分完之后纪律**比原来更硬**，因为它对上了真正的认识论红线：**不能用生成假设的
那批材料去确证同一个假设**。第一版要求人人先冻协议，反而把这条红线糊掉了 ——
它让"先探索后确证"这条唯一诚实的路径无法表达，于是探索只能伪装成确证。

`exploratory` + 写了 `closure_discharges` = 硬拒。这一条就是 anti-HARKing 在
取样这一侧的机械形态。

## findings 是一等产物，不是副产品

英国饮食那趟最有价值的一段思考是：发现两条 claim 互相矛盾，并找到第三条作为
理论中介协调它们。第一版设计里它只是一条 guideline —— 不是产物、不进 KB、
writing 读不到。

所以 findings 进契约：每条挂证据、声明推理类型，**溯因型必须列竞争解释**。
"我想到的唯一解释"不是结论。

## 两道门怎么分（2026-08-23 重划）

判据**一个字都不重写**：两道门调同一批函数，冻结门查的是写入门的超集。
分界线是**这一条判得动判不动，以及绕过它有多大破坏**：

| | 写入门（每次 save，必经） | 冻结门（freeze 时，自愿） |
|---|---|---|
| 查什么 | 只看这份 draft 自己就能判的 | 要**对照项目其他事实**才能判的 |
| 具体 | mode 合法、取样纪律痕迹、findings 结构、探索不许勾账、覆盖度算术 | 勾账键对不对得上冻结 prereg、confirmatory 的协议与分母 |

⚠️ 为什么 anti-HARKing 那条必须站在**写入面**：`core.prereg_commitments.
_scan_result_metadata` 收兑现记录时**不看产物冻没冻** —— 它按类型
（`carries_discharge_ledger`）扫全部产物的 metadata。所以一份 exploratory 记录
只要写了 `closure_discharges`、哪怕从不调 `freeze_artifact`，那些勾账**照样进账本**。
把这条挂在冻结门上，等于留了一条"不冻结就什么都不查"的近路 —— 与 2026-08-23
derivation 那次事故（模型 9 次 check_step、2 次 save、0 次 freeze）同一个形状。

**闸放哪层按对手是谁推。对手是"不冻结就交付"，闸就不能只长在冻结上。**

## 判据形态：问数据，不问自证

闸全部走"字段必须存在且非空"而不是"某个布尔标记是不是 true" —— 框架文档已写明
后者"逼着每个工具自己写一个 passed: true，判据变成自证"。

空判定的唯一实现在 `shared/lib/metadata_contract`：`0` 与 `False` 是**合法取值**
（真的什么都没排除时 `n_excluded: 0` 就是对的），把它当"没填"会逼模型编一个
非零数字出来 —— 闸反过来制造它要防的行为。

⚠️ 这些闸只保证**纪律的痕迹在**，不保证研究**做得好**。后者是语义判断，归
reviewer。这条边界不许被"多加几个闸更严格"的直觉侵蚀：闸一旦开始查内容质量，
就会重演"门要一个形式、模型就生产这个形式"（英国饮食那趟 846 万 tokens 的病）。

## 不在这里的：credibility 与 verdicts

它们**不是 metadata 契约**，是正文的段落（`## Verdict` / `## Credibility`，
见 harness 的 expected_outputs）。2026-08-19 QC 退役时，那两条
`artifact_content_regex`（查正文有没有 Credibility 段、有没有裁决词）被翻译成了
`required_metadata` 里的两个 metadata 键 —— **换了执法位置，也换了字段的落点**，
而给模型的契约没跟着改。后果是模型每份记录写两遍（正文一份、metadata 一份），
两份必然各自演化；而 `metadata.verdicts` 全仓**没有任何读者**（writing 的输入
审计查的是 `closure_discharges` 或单数 `verdict`，实测 5 份真跑无一写单数）。

裁决的落点是正文与 `closure_discharges`，不是再复制一个键。
"""
from __future__ import annotations

import csv
import io
import json
import math
from typing import Any

from shared.lib.metadata_contract import dig as _dig, is_filled as _is_filled

_LOG_TYPE = "observation_log"
_RESULTS_TYPE = "observation_results"

RESULT_TABLE_CONTRACTS: dict[str, dict[str, Any]] = {
    "event_timeline": {
        "required_roles": ("item_id", "lane", "start", "label", "kind", "source_ref"),
        "numeric_roles": ("start",),
        "semantic_contract": "event_timeline_contract",
    },
    "claim_evidence_matrix": {
        "required_roles": (
            "claim",
            "dimension",
            "verdict",
            "evidence_strength",
            "source_ref",
        ),
        "numeric_roles": ("evidence_strength",),
        "semantic_contract": "evidence_matrix_contract",
    },
    "categorical_composition": {
        "required_roles": ("category", "component", "value", "source_ref"),
        "numeric_roles": ("value",),
        "semantic_contract": "composition_contract",
    },
    "diverging_comparison": {
        "required_roles": ("label", "value", "source_ref"),
        "numeric_roles": ("value",),
        "semantic_contract": "diverging_contract",
    },
    "radar_profile": {
        "required_roles": ("axis", "value", "group", "source_ref"),
        "numeric_roles": ("value",),
        "semantic_contract": "radar_contract",
    },
}

EXPLORATORY = "exploratory"
CONFIRMATORY = "confirmatory"
KNOWN_MODES = (EXPLORATORY, CONFIRMATORY)

#: 两种模式都欠的取样纪律痕迹 —— 写入门与冻结门都查。
STRUCTURAL_REQUIRED_PATHS: tuple[str, ...] = (
    "mode",
    "coverage.sources_consulted",
    "adversarial_search.queries",
)

#: 只有 confirmatory 欠的：协议先冻 + 覆盖度带分母。冻结门查。
CONFIRMATORY_REQUIRED_PATHS: tuple[str, ...] = (
    "search_protocol.frozen_ref",
    "search_protocol.inclusion_criteria",
    "search_protocol.exclusion_criteria",
    "coverage.n_screened",
    "coverage.n_included",
    "coverage.n_excluded",
)


def _audit_findings(findings: Any) -> list[str]:
    """findings 的结构纪律。

    只查**声称溯因的结论有没有列竞争解释**，不查解释好不好 —— 后者归 reviewer。
    模型当然可以把所有 finding 都标成 descriptive 来绕开这条；那样它就没有在
    声称解释任何事，而一份满是"描述性发现"却在下结论的 log，reviewer 看得见。
    机械层保证**声称了就得有结构**，语义层判**声称得诚不诚实**。
    """
    problems: list[str] = []
    if not isinstance(findings, list):
        return ["findings 必须是列表"]
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            problems.append(f"findings[{index}] 必须是对象")
            continue
        label = finding.get("id") or f"findings[{index}]"
        if not _is_filled(finding.get("statement")):
            problems.append(f"{label} 缺 statement（这条发现到底说了什么）")
        if not _is_filled(finding.get("evidence")):
            problems.append(f"{label} 缺 evidence（这条发现立在什么材料上）")
        if str(finding.get("inference_type") or "").strip().lower() == "abductive":
            if not _is_filled(finding.get("competing_explanations")):
                problems.append(
                    f"{label} 声明为溯因（abductive）却没列 competing_explanations"
                    " —— 我想到的唯一解释不是结论"
                )
    return problems


def audit_record_shape(metadata: Any) -> dict[str, Any]:
    """只看这份 metadata 自己就能判的那部分 —— 写入门与冻结门共用。

    返回 {mode, missing, reasons, advisories}。不读盘、不碰 state：判据只依赖
    draft 本身，所以它在产物还没落盘的那一刻就能跑。

    两档（判决拆除批 3w，docs/verdict_demolition/verdicts_writing_nodes.md
    observation 节）：
      reasons    —— 仍拒绝存盘（B：mode 契约、anti-HARKing、覆盖度账自洽）
      advisories —— 照存盘，未过项写进产物 metadata.advisories（212 取样纪律
                    痕迹 / 252 findings 非空 / 180/182/185 findings 结构 降格：
                    save gate 拦存盘比拒绝渲染更重一档——科学家连草稿都存不下）
    """
    if not isinstance(metadata, dict):
        metadata = {}
    mode = str(_dig(metadata, "mode") or "").strip().lower()
    reasons: dict[str, str] = {}
    advisories: dict[str, str] = {}

    if mode not in KNOWN_MODES:
        reasons["mode"] = (
            f"metadata.mode 必须是 {'/'.join(KNOWN_MODES)} 之一（当前：{mode or '缺失'}）。"
            "两种模式的纪律不同，也决定这份记录能不能勾除闭合条目。"
        )

    missing = [p for p in STRUCTURAL_REQUIRED_PATHS
               if not _is_filled(_dig(metadata, p))]
    if missing:
        advisories["sampling_discipline"] = "取样纪律的记录不完整：" + "、".join(missing)

    # ── anti-HARKing 的机械形态 ────────────────────────────────────────────
    # 探索出来的模式不能用同一批材料确证自己。这一条是本节点纪律的核心：
    # 它让"先探索、后确证"这条唯一诚实的路径可以被表达，代价是探索这一趟
    # 不许关问题。
    #
    # 站在写入面而不是冻结面：账本收兑现记录时不看冻没冻（见模块头 ⚠️），
    # 挂在冻结门上就留了"不冻结就什么都不查"的近路。
    if mode == EXPLORATORY:
        discharges = _dig(metadata, "closure_discharges")
        if isinstance(discharges, dict) and discharges:
            reasons["exploratory_cannot_close"] = (
                "exploratory run 写了 closure_discharges："
                f"{'、'.join(sorted(discharges))}。探索模式不预先冻结取样标准，"
                "用它勾账等于用生成假设的材料确证同一个假设。"
                "把这些发现交给 Analysis 立成研究问题并冻结闭合条件，再起一趟 "
                "confirmatory run 兑现。"
            )
        measurements = _dig(metadata, "measured_metrics")
        if isinstance(measurements, dict) and measurements:
            reasons["exploratory_cannot_measure"] = (
                "exploratory run 写了 measured_metrics —— 同上：预注册承诺的量"
                "只能由 confirmatory run 兑现。"
            )

    # 覆盖度账必须自洽：筛过的不能少于纳入+排除。对不上说明账是拼出来的，
    # 而不是从检索过程里记出来的。
    screened = _dig(metadata, "coverage.n_screened")
    included = _dig(metadata, "coverage.n_included")
    excluded = _dig(metadata, "coverage.n_excluded")
    if all(isinstance(v, int) for v in (screened, included, excluded)):
        if screened < included + excluded:
            reasons["coverage_arithmetic"] = (
                f"覆盖度账对不上：筛查 {screened} < 纳入 {included} + 排除 {excluded}"
            )

    # findings 是一等产物：两种模式都要有，且结构上站得住。缺了/结构不齐
    # 如实记 advisory（判决拆除 252/180/182/185 降格），不再拦存盘。
    findings = _dig(metadata, "findings")
    if not _is_filled(findings):
        advisories["no_findings"] = (
            "findings 为空。哪怕这一趟只是确证既有假设，也要写下你**看到了什么** ——"
            "证据之间的张力、来源强度的差异、没预期到的模式。"
            "一份只有勾选框的记录，把这一趟真正的收获丢在了 prose 里。"
        )
    else:
        problems = _audit_findings(findings)
        if problems:
            advisories["findings_structure"] = "；".join(problems)

    return {"mode": mode or None, "missing": missing,
            "reasons": reasons, "advisories": advisories}


def _frozen_closure_keys(state: Any) -> set[str]:
    """冻结预注册里所有闭合条目的合法键。拿不到就返回空集（不拦）。

    拿不到 ≠ 键错了：非 Project 独立运行、协议还没冻结时都可能读不到。
    这道闸只在能确知合法键时才判 —— 拦不该拦的会把一次读盘失败变成
    "这个节点交不了差"。
    """
    try:
        from core.prereg_commitments import frozen_questions

        return {
            item.key
            for question in frozen_questions(state).values()
            for item in question.closure
        }
    except Exception:
        return set()


def audit_observation_log(state: Any, artifact_id: str) -> dict[str, Any]:
    """冻结前审计 —— 写入门那套的**超集**，加上要对照项目事实才能判的两条。

    返回 {passed, mode, missing, reasons}，不抛异常。
    """
    record = state.read_artifact(artifact_id)
    if not isinstance(record, dict) or record.get("type") != _LOG_TYPE:
        return {
            "passed": False, "mode": None, "missing": [], "advisories": {},
            "reasons": {"artifact": f"{artifact_id} 不是 {_LOG_TYPE}"},
        }

    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    shape = audit_record_shape(metadata)
    mode, reasons = shape["mode"], dict(shape["reasons"])
    advisories = dict(shape["advisories"])
    missing = list(shape["missing"])

    # ── 只有对照项目其他事实才判得动的两条 ────────────────────────────────
    # confirmatory 协议/分母字段（判决拆除 310 降格）：缺了如实记 advisory，
    # 不再拒绝冻结——covered by S2：缺分母的覆盖度账 reviewer 看得见。
    if mode == CONFIRMATORY:
        confirmatory_missing = [p for p in CONFIRMATORY_REQUIRED_PATHS
                                if not _is_filled(_dig(metadata, p))]
        if confirmatory_missing:
            missing += confirmatory_missing
            advisories["sampling_discipline"] = (
                "取样纪律的记录不完整：" + "、".join(missing))

    # 勾账的键必须逐字对上冻结预注册。
    #
    # 2026-08-19 真跑抓到：observation 老老实实勾了 12 条账，键全写成
    # `Q1_DISCOURSE_EVIDENCE`，而预注册里的 id 是 `DISCOURSE_EVIDENCE` ——
    # 模型自己加了问题号前缀。交集为空，**12 条兑现一条都不算数**。
    #
    # 后果是静默的：metadata 语法合法、冻结闸放行、节点报告"11/12 已兑现"，
    # 只有下游对账时才发现账本认为零兑现，于是 writing 判"无可报告"拒绝开写。
    # 而那时错误已经冻进不可变记录里了。
    #
    # 病根不是模型乱来，是**契约没送到勾账的人手上**：harness 只说"勾除挂
    # 证据"，从没说键必须逐字抄 prereg 的 `id`。所以这里不只拦，还把合法键
    # 逐个列出来 —— 报错要能直接照着改。
    discharge_block = _dig(metadata, "closure_discharges")
    if isinstance(discharge_block, dict) and discharge_block:
        legal = _frozen_closure_keys(state)
        if legal:
            unknown = sorted(k for k in discharge_block if k not in legal)
            if unknown:
                reasons["unknown_closure_keys"] = (
                    f"这些勾账键在冻结预注册里不存在：{'、'.join(unknown)}。"
                    f"合法键（逐字照抄，不要加问题号前缀）：{'、'.join(sorted(legal))}。"
                    "键对不上的兑现记录，账本一条都不认 —— 而且不会报错，"
                    "只会在下游写作时表现为「零兑现」。"
                )

    return {"passed": not reasons, "mode": mode,
            "missing": missing, "reasons": reasons, "advisories": advisories}


# ── 两道门：写入必经、冻结自愿，判据同源 ────────────────────────────────────
from shared.tools.library.artifacts_extra import (  # noqa: E402
    register_freeze_gate as _register_freeze_gate,
    register_save_gate as _register_save_gate,
)

#: 契约与拒绝是同一件事的两面 —— 渲染进模型真正调用的那个工具的说明里，
#: 而不是只在被拒时才出现（「契约必须送到调用方」）。
_SAVE_CONTRACT = {
    "mode": (
        f"{'/'.join(KNOWN_MODES)} 之一。exploratory=发现（不要求先冻协议，"
        "⛔**不许**写 closure_discharges / measured_metrics）；"
        "confirmatory=兑现（协议先冻、覆盖度带分母，可以勾账）。"
    ),
    "coverage.sources_consulted": "看了哪些源 —— 哪怕在探索，也要能说清视野覆盖到哪。",
    "adversarial_search.queries": (
        "主动找反证用的反向 query。**找不到反证是结论，找都没找不是** —— "
        "两者在结果上都表现为「没有反证材料」，只有过程记录能区分。"
        "一条都没找到就写 n_contradicting_found: 0（合法取值，别编一个非零数）。"
    ),
    "findings": (
        "这一趟**看到了什么**，结构化。每条挂 statement + evidence + "
        "inference_type（descriptive / inductive / abductive）；"
        "**溯因型必须列 competing_explanations** —— 我想到的唯一解释不是结论。"
    ),
}


#: 拒绝必须给出**正路** —— 只说"不许"，调用方唯一能做的就是重试。
#: 两道门拒的是同一条规则，出口说明就只能有一份。
_EXPLORATORY_WAY_OUT = (
    "exploratory 模式不许写 closure_discharges / measured_metrics —— "
    "探索产出交给 Analysis 立成研究问题、冻结闭合条件，"
    "再起一趟 confirmatory run 兑现。"
)


def _way_out(reasons: dict[str, str]) -> str:
    """只在真的撞上那条规则时才讲它的正路。

    无条件附上等于给每次拒绝都塞一段不相干的话：confirmatory 少写了个分母，
    却被告知"exploratory 不许勾账" —— 报错一旦开始讲不相干的规则，
    调用方就学会跳过报错正文。
    """
    hit = {"exploratory_cannot_close", "exploratory_cannot_measure"} & set(reasons)
    return _EXPLORATORY_WAY_OUT if hit else ""


def _record_advisories_on_draft(draft: dict, advisories: dict[str, str]) -> None:
    """降格项的机械落点：未过项写进**要落盘的那份** metadata（照存盘，如实可见）。

    run_save_gate 传进来的 metadata 与 save_artifact 将写盘的是同一个 dict——
    改它即改账。metadata 不是 dict 时说明别的 blocking 判据必然在场，不强写。
    """
    metadata = (draft or {}).get("metadata")
    if advisories and isinstance(metadata, dict):
        existing = metadata.get("advisories")
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(advisories)
        metadata["advisories"] = merged


def _observation_log_save_gate(state, draft: dict) -> dict[str, Any]:
    audit = audit_record_shape((draft or {}).get("metadata"))
    _record_advisories_on_draft(draft, audit["advisories"])
    if not audit["reasons"]:
        return {}
    return {
        "failures": dict(audit["reasons"]),
        "missing": audit["missing"],
        "mode": audit["mode"],
        "advisories": audit["advisories"],
        "hint": (
            "这份记录**长什么样**是每次写入都查的（冻结是自愿动作，不能把纪律"
            "挂在自愿动作上）。metadata 必须含 mode；取样纪律痕迹与 findings "
            "结构（statement + evidence；abductive 列 competing_explanations）"
            "缺了会照存盘、如实记进 metadata.advisories。"
            + _way_out(audit["reasons"])
        ),
    }


def _observation_log_freeze_gate(state, artifact_id, record):
    audit = audit_observation_log(state, artifact_id)
    state.append_transcript(
        "observation_pre_freeze_gate",
        artifact_id=artifact_id, mode=audit["mode"], passed=audit["passed"],
        missing=audit["missing"], reasons=audit["reasons"],
        advisories=audit["advisories"],
    )
    if audit["passed"]:
        return {}
    mode = audit["mode"]
    return {
        "failures": dict(audit["reasons"]),
        "missing": audit["missing"],
        "mode": mode,
        "reasons": audit["reasons"],
        "advisories": audit["advisories"],
        "hint": (
            f"本 run 模式={mode or '未声明'}。metadata 必须含 mode；勾账的键要"
            "逐字照抄冻结 prereg 里的 `id`，对不上账本一条都不认。"
            + _way_out(audit["reasons"])
        ),
    }


def _parse_result_rows(content: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Parse a small display-ready table without coupling Observation to Postprocess."""

    text = str(content or "").strip()
    if not text:
        raise ValueError("content 为空")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if (
        isinstance(payload, list)
        and payload
        and all(isinstance(row, dict) for row in payload)
    ):
        columns = list(payload[0])
        if any(set(row) != set(columns) for row in payload):
            raise ValueError("JSON 每行必须有相同字段")
        return columns, [dict(row) for row in payload]

    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",\t;")
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        rows = [dict(row) for row in reader]
    except (csv.Error, TypeError) as exc:
        raise ValueError("content 必须是 CSV/TSV 或 JSON list of objects") from exc
    columns = list(reader.fieldnames or [])
    if not columns or not rows:
        raise ValueError("结果表必须有表头和至少一行")
    return columns, rows


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def audit_observation_results_shape(draft: Any) -> dict[str, Any]:
    """Validate evidence-bearing rows, not a self-attested ``display_ready`` flag.

    两档（判决拆除批 3w，observation 节）：
      failures   —— 仍拒绝（C 契约：table_kind/schema/log_ref/内容可解析/列真实
                    存在/数值可画、受控词表、账自洽）
      advisories —— 照存盘，写进 metadata.advisories（语义角色/顺序声明/逐行
                    source_ref/矩阵完整性等 505/510/533/553/557/561/583/585/
                    607/616/626/684 降格）
    已整条删除（档一）：545 label ≤64 字符、644 reference_value 必须抄 0、
    660 雷达 ≥3 轴、662 value_range 必须抄 [0,1]、666 direction_consistent
    必须抄 true——抄固定字面量框架不验/任意审美阈值。
    """

    draft = draft if isinstance(draft, dict) else {}
    metadata = draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {}
    failures: dict[str, str] = {}
    advisories: dict[str, str] = {}
    table_kind = str(metadata.get("table_kind") or "").strip()
    contract = RESULT_TABLE_CONTRACTS.get(table_kind)
    if contract is None:
        failures["table_kind"] = (
            "metadata.table_kind 必须是 " + " / ".join(sorted(RESULT_TABLE_CONTRACTS))
        )
        return {"passed": False, "table_kind": table_kind or None,
                "failures": failures, "advisories": advisories}

    if metadata.get("schema_version") != 1:
        failures["schema_version"] = "metadata.schema_version 必须是整数 1"
    if not _is_filled(metadata.get("observation_log_ref")):
        failures["observation_log_ref"] = "必须指向本轮 observation_log"
    roles = (
        metadata.get("field_roles")
        if isinstance(metadata.get("field_roles"), dict)
        else {}
    )
    missing_roles = [
        role for role in contract["required_roles"] if not _is_filled(roles.get(role))
    ]
    if missing_roles:
        advisories["field_roles"] = "缺必需语义角色：" + "、".join(missing_roles)

    semantic_name = str(contract["semantic_contract"])
    semantic = metadata.get(semantic_name)
    if not isinstance(semantic, dict) or not semantic:
        advisories[semantic_name] = f"metadata.{semantic_name} 缺失或为空"
        semantic = {}

    try:
        columns, rows = _parse_result_rows(draft.get("content"))
    except ValueError as exc:
        failures["content"] = str(exc)
        return {"passed": False, "table_kind": table_kind,
                "failures": failures, "advisories": advisories}
    unknown = sorted({str(field) for field in roles.values() if str(field) not in columns})
    if unknown:
        failures["field_roles_unknown"] = (
            "field_roles 引用了不存在的列：" + "、".join(unknown)
        )

    for role in contract["numeric_roles"]:
        field = roles.get(role)
        if field and any(_finite_number(row.get(field)) is None for row in rows):
            failures[f"numeric_role.{role}"] = (
                f"角色 {role} 的列 {field!r} 必须全是有限数值"
            )

    source_field = roles.get("source_ref")
    if source_field and any(not _is_filled(row.get(source_field)) for row in rows):
        # 判决拆除 533 降格：逐行溯源是义务，缺行如实记录交 reviewer。
        advisories["source_ref"] = (
            "有行缺可核验 source_ref（只给整表一个泛化来源）——逐行溯源义务未兑现"
        )

    # 「图内 label ≤64 字符」已删（判决拆除 545 档一：任意审美阈值）。

    if table_kind == "event_timeline":
        lane_order = semantic.get("lane_order")
        if not isinstance(lane_order, list) or not lane_order:
            advisories["event_timeline_contract.lane_order"] = "未显式给出非空 lane_order"
        elif set(str(value) for value in lane_order) != {
            str(row.get(roles.get("lane"))) for row in rows
        }:
            advisories["event_timeline_contract.lane_order"] = (
                "lane_order 未逐一覆盖结果表里的 lane"
            )
        if not _is_filled(semantic.get("time_label")):
            advisories["event_timeline_contract.time_label"] = "未声明时间轴含义/单位"
        seen_ids: set[str] = set()
        for index, row in enumerate(rows):
            item_id = str(row.get(roles.get("item_id")) or "")
            kind = str(row.get(roles.get("kind")) or "").strip().lower()
            start = _finite_number(row.get(roles.get("start")))
            if not item_id or item_id in seen_ids:
                failures[f"row.{index + 1}.item_id"] = "item_id 必须非空且唯一"
            seen_ids.add(item_id)
            if kind not in {"point", "interval"}:
                failures[f"row.{index + 1}.kind"] = "kind 只能是 point / interval"
            if kind == "interval":
                end_field = roles.get("end")
                end = _finite_number(row.get(end_field)) if end_field else None
                if end is None or start is None or end <= start:
                    failures[f"row.{index + 1}.end"] = (
                        "interval 必须有有限 end 且 end > start"
                    )
    elif table_kind == "claim_evidence_matrix":
        claim_order = semantic.get("claim_order")
        dimension_order = semantic.get("dimension_order")
        if not isinstance(claim_order, list) or not claim_order:
            advisories["evidence_matrix_contract.claim_order"] = "未显式给出 claim_order"
        if not isinstance(dimension_order, list) or not dimension_order:
            advisories["evidence_matrix_contract.dimension_order"] = (
                "未显式给出 dimension_order"
            )
        verdict_field = roles.get("verdict")
        allowed = {"supports", "contradicts", "mixed", "inconclusive", "missing"}
        invalid = sorted({str(row.get(verdict_field) or "") for row in rows} - allowed)
        if invalid:
            failures["verdict"] = (
                "verdict 只能使用受控词表；无效值：" + "、".join(invalid)
            )
        strength_field = roles.get("evidence_strength")
        strengths = [_finite_number(row.get(strength_field)) for row in rows]
        if any(value is not None and not 0 <= value <= 1 for value in strengths):
            failures["evidence_strength"] = "evidence_strength 必须在 [0, 1]"
        keys = [
            (
                str(row.get(roles.get("claim"))),
                str(row.get(roles.get("dimension"))),
            )
            for row in rows
        ]
        if len(set(keys)) != len(keys):
            # 判决拆除 607 降格：证据真实冲突时如实保留冲突单元格是合法记录——
            # 提醒裁决，不再没收。
            advisories["matrix_duplicates"] = (
                "claim/dimension 存在重复单元格——若是证据冲突，如实保留并说明；"
                "若可裁决，上游裁决为单一 verdict"
            )
        expected = {
            (str(claim), str(dimension))
            for claim in claim_order or []
            for dimension in dimension_order or []
        }
        if expected and set(keys) != expected:
            advisories["matrix_rectangular"] = (
                "矩阵不完整；无证据的单元格建议显式写 verdict=missing"
            )
    elif table_kind == "categorical_composition":
        mode = str(semantic.get("mode") or "").strip().lower()
        if mode not in {"absolute", "share"}:
            failures["composition_contract.mode"] = "mode 只能是 absolute / share"
        if not isinstance(semantic.get("component_order"), list) or not semantic.get(
            "component_order"
        ):
            # 判决拆除 626 降格
            advisories["composition_contract.component_order"] = (
                "未显式给出非空 component_order"
            )
        value_field = roles.get("value")
        if any((_finite_number(row.get(value_field)) or 0) < 0 for row in rows):
            failures["composition_values"] = "组成值不得为负"
        if mode == "share":
            totals: dict[str, float] = {}
            for row in rows:
                category = str(row.get(roles.get("category")))
                totals[category] = totals.get(category, 0.0) + float(
                    _finite_number(row.get(value_field)) or 0
                )
            bad = {key: value for key, value in totals.items() if abs(value - 1.0) > 0.01}
            if bad:
                failures["composition_totals"] = "share 模式每个 category 必须合计为 1"
    elif table_kind == "diverging_comparison":
        # 「必须声明 reference_value: 0」已删（判决拆除 644 档一：抄固定字面量
        # 框架不验）。零点两侧检查（654）保留——它带 allow_one_sided 显式布尔
        # 声明出口，是档二的正确形态（出口不由关键词把守）。
        value_field = roles.get("value")
        values = [_finite_number(row.get(value_field)) for row in rows]
        finite_values = [value for value in values if value is not None]
        if semantic.get("allow_one_sided") is not True and not (
            any(value < 0 for value in finite_values)
            and any(value > 0 for value in finite_values)
        ):
            failures["diverging_values"] = (
                "发散比较必须在零点两侧都有值；单侧比较需显式 allow_one_sided: true"
            )
    elif table_kind == "radar_profile":
        # 「≥3 轴」（660：任意审美阈值，用图型美学没收合法数据——轴少正解是
        # 换 bar，归模型判）、「value_range 必须抄 [0,1]」（662）、
        # 「direction_consistent 必须抄 true」（666）均已删（判决拆除档一：
        # 抄固定字面量框架不验）。真实值域检查（radar_values ∈ [0,1]）保留。
        axis_order = semantic.get("axis_order")
        value_field = roles.get("value")
        values = [_finite_number(row.get(value_field)) for row in rows]
        if any(value is not None and not 0 <= value <= 1 for value in values):
            failures["radar_values"] = "雷达值必须在 [0, 1]"
        axis_field = roles.get("axis")
        group_field = roles.get("group")
        expected_axes = {str(value) for value in axis_order or []}
        group_axes: dict[str, list[str]] = {}
        for row in rows:
            group = str(row.get(group_field))
            group_axes.setdefault(group, []).append(str(row.get(axis_field)))
        if expected_axes and any(
            set(axes) != expected_axes or len(axes) != len(expected_axes)
            for axes in group_axes.values()
        ):
            # 判决拆除 684（obs）降格
            advisories["radar_rectangular"] = (
                "有 group 未恰好包含 axis_order 中的每个轴一次"
            )

    return {
        "passed": not failures,
        "table_kind": table_kind,
        "row_count": len(rows),
        "columns": columns,
        "failures": failures,
        "advisories": advisories,
    }


def _observation_results_save_gate(state, draft: dict) -> dict[str, Any]:
    audit = audit_observation_results_shape(draft)
    _record_advisories_on_draft(draft, audit.get("advisories") or {})
    if audit["passed"]:
        return {}
    return {
        "failures": audit["failures"],
        "advisories": audit.get("advisories") or {},
        "table_kind": audit.get("table_kind"),
        "hint": (
            "observation_results 必须是可解析、列真实存在、数值可画的表；"
            "语义角色/顺序声明/逐行 source_ref 缺了会照存盘、如实记进 "
            "metadata.advisories。field_roles 只声明列的科学含义，"
            "颜色、坐标布局和标签位置由 Postprocess 决定。"
        ),
    }


def _observation_results_freeze_gate(state, artifact_id, record):
    audit = audit_observation_results_shape(record)
    failures = dict(audit["failures"])
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    log_ref = str(metadata.get("observation_log_ref") or "")
    linked = state.read_artifact(log_ref) if log_ref else None
    if not isinstance(linked, dict) or linked.get("type") != _LOG_TYPE:
        failures["observation_log_ref"] = "冻结前必须能解析到本轮 observation_log"
    state.append_transcript(
        "observation_results_pre_freeze_gate",
        artifact_id=artifact_id,
        table_kind=audit.get("table_kind"),
        passed=not failures,
        failures=failures,
        advisories=audit.get("advisories") or {},
    )
    return {} if not failures else {"failures": failures}


_RESULTS_SAVE_CONTRACT = {
    "content": "CSV/TSV 或 JSON records；每行一个可核验观察，不得夹长篇 prose。",
    "metadata.schema_version": "整数 1。",
    "metadata.table_kind": (
        "event_timeline / claim_evidence_matrix / categorical_composition / "
        "diverging_comparison / radar_profile。"
    ),
    "metadata.field_roles": (
        "把语义角色映射到真实列名；每种 table_kind 的必需角色由门返回。"
    ),
    "metadata.observation_log_ref": "本轮 observation_log artifact id。",
    "metadata.<kind>_contract": (
        "顺序、单位、值域等科学语义；不包含颜色或手工坐标布局。"
    ),
}


_register_save_gate(_LOG_TYPE, _observation_log_save_gate,
                    content_contract=_SAVE_CONTRACT)
_register_freeze_gate(_LOG_TYPE, _observation_log_freeze_gate)
_register_save_gate(
    _RESULTS_TYPE,
    _observation_results_save_gate,
    content_contract=_RESULTS_SAVE_CONTRACT,
)
_register_freeze_gate(_RESULTS_TYPE, _observation_results_freeze_gate)
