"""Analysis 侧的研究问题视图 —— 解析权在框架，这里只做节点自己的审计。

**解析不在这里。** `core.prereg_commitments` 是研究问题与闭合条件的唯一解析器，
冻结闸、欠账账本、关闭门禁、experiment 的结论绑定、本节点的收尾自检全都调它。
一个问题只有一个真相源；本模块自己再写一遍正则，就是第二个会各自演化的答案。

本模块只回答三件节点自己的事：
  1. 这份协议草稿在**声明形态**上齐不齐（供 freeze 前自检，与冻结闸同判据）；
  2. 本项目**有没有带命题的问题**（= 假设类判据适不适用）；
  3. 哪些条目要进 Analysis 的裁决账。

## 为什么没有「这是不是假设」这个字段

**写了 `proposition` 的问题就是假设，不写就不是。** 结构本身就是声明。

第一版设计里我加过一个 `decides_a_proposition: yes|no` 开关，那是多余的一层：
多一个可以填错的字段，也多一条"声明成 no 躲开判据"的绕行路径。现在要躲判据
只有一个办法 —— 别写命题；而不写命题就意味着你没在裁决任何命题，那本来就
不需要判据。声明与后果天然一致，没有缝。
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.prereg_commitments import (
    closure_shape_hint,
    NUMERIC,
    ResearchQuestion,
    parse_questions,
)
from core.state import State

#: 与 core 的段落标题保持一致（core 是权威；这里只用于"写在段外"的检测）
_QUESTION_HEADER_RE = re.compile(
    r"^#{2,5}\s*([QH]\d+)\s*(?:[:：\-—．.、]|\s)\s*(.*)$",
    re.MULTILINE | re.IGNORECASE,
)

#: 空话检测 —— 机械层对文本**只能查「在不在、是不是空话」，不能查长度**。
#: 实测（2026-08-16 A/B 跑）：`output_kind: 一张相图` 四个字被 `>=6 字` 判为过短。
#: 那是完全合格的产出声明。查长度就是查形式，查形式就会逼出凑字 ——
#: 跟旧版逼出凑数字是同一个病，只是换了个地方。
_VAGUE_ONLY = re.compile(
    r"^\W*(?:待定|TBD|N/?A|无|不适用|视情况(?:而定)?|后续(?:再)?(?:确定|补充)|"
    r"see above|同上|as needed|依实际情况|按需|略|\?+)\W*$",
    re.IGNORECASE)


def _blank(value: object) -> bool:
    text = str(value or "").strip()
    return not text or bool(_VAGUE_ONLY.match(text))


# ── 取料 ──────────────────────────────────────────────────────────────


def latest_prereg_content(state: State) -> str:
    """最新一份 pre_registration 正文；冻结的优先（它才是生效的协议）。"""
    frozen = ""
    latest = ""
    for entry in state.list_artifacts("pre_registration"):
        record = state.read_artifact(str(entry["id"])) or {}
        content = str(record.get("content") or "")
        if not content:
            continue
        latest = content
        meta = record.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        if isinstance(meta, dict) and meta.get("frozen"):
            frozen = content
    return frozen or latest


def project_expects_hypotheses(content: str) -> bool:
    """本项目有没有**带命题**的研究问题 —— 假设类判据的适用开关。"""
    return any(q.is_hypothesis for q in parse_questions(content).values())


def state_expects_hypotheses(state: State) -> bool:
    return project_expects_hypotheses(latest_prereg_content(state))


def ledger_entry_ids(content: str) -> list[str]:
    """要进 Analysis 裁决账的条目 —— **就是全部研究问题**。

    不再分"假设入册、问题不入册"：一等公民只有一种，账本收的就是它。
    这条一改，三条既有完整性规则（上一版条目不得凭空消失 / ready_candidate
    要求全部已裁决 / 收尾必须出新一版）对没有命题的研究自动全部生效 ——
    不用一条条去接。
    """
    return list(parse_questions(content))


def state_ledger_entry_ids(state: State) -> list[str]:
    return ledger_entry_ids(latest_prereg_content(state))


def stray_question_ids(content: str, questions: dict[str, ResearchQuestion]) -> list[str]:
    """写在 `## Research Questions` 段**之外**的 `### Qn:` 标题。

    模型很自然会把 Q2 写到某个上文段落后面 —— 那时它落在段外，解析器看不见它，
    于是这个问题**静默消失**：不报错、不进账、不被审。静默丢弃是最坏的失败
    方式，宁可吵。
    """
    known = {qid.upper() for qid in questions}
    seen = [m.group(1).upper() for m in _QUESTION_HEADER_RE.finditer(content or "")]
    return [qid for qid in dict.fromkeys(seen) if qid not in known]


# ── 审计 ──────────────────────────────────────────────────────────────


def assess_research_questions(content: str) -> dict[str, Any]:
    """协议草稿的声明形态审计。纯函数，便于单测穷举。

    判据与冻结闸同源（都读 core 的解析结果），差别只是**时机**：这里在
    freeze 之前给出可修的反馈，冻结闸在不可逆的那一刻兜底。两处不许有
    第二套判据 —— 否则模型会拿着这里的 pass 撞上那里的 fail，无从理解。
    """
    questions = parse_questions(content)

    if not questions:
        return {
            "applicable": True,
            "passed": False,
            "n_questions": 0,
            "n_hypotheses": 0,
            "problems": [
                "协议里没有任何研究问题。`## Research Questions` 段下写 "
                "`### Q1: <要回答什么>`，给 output_kind、"
                "【判断某句话对不对时才写】proposition，以及闭合条件。"
                "**至少一个** —— 这是所有科研范式的共性底线。\n"
                + closure_shape_hint()
            ],
            "questions": [],
            "reason": "协议里没有任何研究问题",
        }

    problems: list[str] = []
    rows: list[dict[str, Any]] = []

    for qid, q in questions.items():
        issues: list[str] = []
        if not q.title.strip():
            issues.append("标题里要写清这个问题要回答/产出什么")
        if _blank(q.output_kind):
            issues.append(
                "output_kind 缺失或是空话：一句话说清**产出是什么**"
                "（一条命题的裁决 / 一个数 / 一张图或库 / 一个方法 / "
                "一个解释 / 一次复现 / 一个推导 …）。简洁没问题，「一张相图」就够。"
            )
        if not [a for a in q.assumptions if not _blank(a)]:
            issues.append(
                "缺 `- assumption:` —— 每个研究问题都要写明**它预设了什么**（可多条）。"
                "这不是假设专属：探索题同样有预设（「扫这个区间」预设了答案在区间里）；"
                "提问方式本身也会偷偷把待检验的前提当成背景 —— 「负面评价在什么时期形成"
                "并固化？」就预设了它确实形成并固化过。写出来，让审查看得见。"
            )
        if not q.closure:
            issues.append(
                "没有闭合条件 —— 必须承诺「怎样算答完」，冻结后不可改。"
                "数值条 `- metric/comparison/threshold`，或"
                "陈述条 `- statement: <可观察的条件>`；两类地位平等"
            )
        elif q.is_hypothesis and not any(
            it.kind == NUMERIC or (it.statement or "").strip() for it in q.closure
        ):
            issues.append("写了 proposition（= 这是一条假设），闭合条件里得有能裁决它的那一条")
        rows.append({
            "id": qid, "title": q.title, "output_kind": q.output_kind,
            "is_hypothesis": q.is_hypothesis, "n_closure": len(q.closure),
            "issues": issues, "passed": not issues,
        })
        if issues:
            problems.append(f"{qid}: " + "；".join(issues))

    stray = stray_question_ids(content, questions)
    if stray:
        problems.append(
            "以下问题写在 `## Research Questions` 段之外，解析不到："
            + ", ".join(stray)
            + "。所有 `### Qn:` 小节必须都在该段内 —— 段外的会被静默丢弃"
        )

    n_hyp = sum(1 for q in questions.values() if q.is_hypothesis)
    passed = not problems
    if passed:
        reason = (
            f"{len(questions)} 个研究问题声明齐全"
            f"（{n_hyp} 个带命题 → 走假设判据；{len(questions) - n_hyp} 个"
            "无命题 → 只需兑现闭合条件，不要求数值阈值）"
        )
    else:
        reason = "研究问题声明不完整：" + "；".join(problems[:5])

    return {
        "applicable": True,
        "passed": passed,
        "n_questions": len(questions),
        "n_hypotheses": n_hyp,
        "problems": problems,
        "questions": rows,
        "reason": reason,
    }


def assess_question_plan_coverage(prereg: str, plan: str) -> dict[str, Any]:
    """声明了一个研究问题，research_plan 里就得有步骤在回答它。

    判据故意做得宽：问题编号在计划正文里出现过就算数 —— 不规定必须写在哪一列。
    要的是"这一步为哪个问题服务"这件事被写下来，不是又一种表格格式。
    """
    questions = parse_questions(prereg)
    if not questions:
        return {"applicable": False, "passed": True, "uncovered": [],
                "reason": "协议里没有研究问题，无从对账"}
    if all(q.legacy for q in questions.values()):
        # 历史格式（`## Hypothesis N (Hx)`）的协议已经冻结、计划也早写好了，
        # 而"计划里要写明哪几步服务哪个问题"是 v0.5 才有的要求。对它们
        # 追溯执行只会把在跑的项目判死，而且改不动 —— 不适用。
        return {"applicable": False, "passed": True, "uncovered": [],
                "reason": "legacy 假设格式协议，计划对账要求不追溯适用"}
    if not str(plan or "").strip():
        return {"applicable": True, "passed": False,
                "uncovered": sorted(questions),
                "reason": "没有 research_plan，声明的研究问题全部无人认领"}

    uncovered = [
        qid for qid in questions
        if not re.search(rf"\b{re.escape(qid)}\b", plan, re.IGNORECASE)
    ]
    if not uncovered:
        return {"applicable": True, "passed": True, "uncovered": [],
                "reason": f"{len(questions)} 个研究问题在 research_plan 里都有步骤认领"}
    return {
        "applicable": True, "passed": False, "uncovered": sorted(uncovered),
        "reason": (
            "以下研究问题在 research_plan 里没有任何步骤认领："
            + ", ".join(sorted(uncovered))
            + "。在 computational_workflow 任务表的「对应 falsifier/问题」列（或正文里）"
            "写明哪几步在回答它 —— 声明了要回答、计划里却没人干，等于没声明。"
            "确实这一轮不做 → 从协议里删掉它。"
        ),
    }


# ── 第一层楼：所有研究问题都要过的内容质量判据 ──────────────────────────
#
# 这三件事**不是假设专属的**，它们对任何研究问题都成立：
#   · 这个问题是不是把已知结论换个说法（"把相图测出来"，而 KB 里已经有这张图）
#   · 这个问题值不值得问、问得通不通
#   · 它预设了什么
#
# v0.5 第一版把它们挂在 claim 上，而无命题的问题不产生 claim —— 于是探索型、
# 表征型、解释型研究一项都拿不到（英国饮食 A/B 实测：三个问题只审了 1 个）。
# 那不是"假设降级"，那是把整层质量门连着假设一起拆了。


def assess_question_restatement(state: State) -> dict[str, Any]:
    """每个研究问题是不是已知结论的复述 —— 对**问题**跑，不只对 claim 跑。"""
    from .conclusion_audit import (
        _audit_one,
        _collect_validated_claims,
        _extract_survey_findings,
    )

    questions = parse_questions(latest_prereg_content(state))
    if not questions:
        return {"applicable": False, "passed": True, "flagged": [],
                "reason": "协议里没有研究问题，无从查重"}

    survey = ""
    arts = state.list_artifacts("survey_report")
    if arts:
        survey = str((state.read_artifact(arts[-1]["id"]) or {}).get("content") or "")
    findings = _extract_survey_findings(survey)
    # ⚠️ 只跟**已证实**的结论比。
    #
    # `_collect_validated_claims` 默认把 status="open" 也算进来 —— 那对它原本的
    # 用途（新假设跟已有假设查重）是对的，对这里是错的：新建的 hypothesis claim
    # 默认就是 open，而它的正文正是从这个问题的 proposition 抄来的。于是问题
    # 拿自己刚登记的 claim 跟自己比，100% 重合，被判成「复述已知结论」。
    #
    # 实测（2026-08-17，LJ 课题真跑）：节点交付齐全却因此 blocked，
    # 自己在报告里写「将本 run 刚创建的 hypothesis claim 误判为已知结论」。
    #
    # 语义上也只有 validated 才叫「已知」：open = 提出但未验证，
    # 一个尚未验证的猜想不构成「这个问题已经有人答过了」。
    validated = [c for c in _collect_validated_claims(state)
                 if str(c.get("status") or "").lower() == "validated"]
    if not findings and not validated:
        return {"applicable": False, "passed": True, "flagged": [],
                "reason": "没有 survey finding 也没有 validated claim，无对照可查"}

    flagged: list[dict[str, Any]] = []
    for qid, q in sorted(questions.items()):
        # 问题本身 + 它的命题（有的话）一起查 —— 复述可能藏在任一处
        text = " ".join(x for x in (q.title, q.proposition if q.is_hypothesis else "") if x)
        if not text.strip():
            continue
        hit = _audit_one(qid, text, findings, validated, overlap_threshold=0.72)
        if hit:
            flagged.append(hit)

    if not flagged:
        return {"applicable": True, "passed": True, "flagged": [],
                "reason": f"{len(questions)} 个研究问题均无结论复述标记"}
    return {
        "applicable": True, "passed": False, "flagged": flagged,
        "reason": "疑似复述已知结论：" + "；".join(
            f"{f['label']}: {f['reason'][:110]}" for f in flagged[:3]),
    }


def assess_question_value_coverage(state: State) -> dict[str, Any]:
    """价值/可信性评估必须**覆盖每个研究问题** —— 按问题编号对账。

    机制/预测新颖性那几个维度只有命题才评（对纯探索题要求"机制新颖性"就是
    逼它编）；但"对 KB 冗余不冗余、问得通不通、推不推得动 open question"
    对任何问题都成立，所以覆盖面按问题算，缺谁报谁。
    """
    questions = parse_questions(latest_prereg_content(state))
    if not questions:
        return {"applicable": False, "passed": True, "missing": [],
                "reason": "协议里没有研究问题"}

    arts = state.list_artifacts("hypothesis_innovation_report")
    if not arts:
        return {"applicable": True, "passed": False, "missing": sorted(questions),
                "reason": ("缺 hypothesis_innovation_report —— "
                           f"{len(questions)} 个研究问题一个都没被评估价值/可信性。"
                           "用 score_hypothesis_innovation 对**每个问题**打分"
                           "（label 用问题编号 Q1/Q2…；纯探索题的机制/预测维度可留空）")}
    body = str((state.read_artifact(arts[-1]["id"]) or {}).get("content") or "")
    # 认**事实**，不认序列化格式。实测（2026-08-16 单问题课题）：模型把评估
    # 写成了 markdown 表格（"### Q1: …" + G/D/M/P 标 N/A + R/Q/I 打分），
    # 完全合格，但只认 `"label": "Q1"` 的正则判它「没评」。
    # 报告是价值评估专用产物，问题编号在里面出现过就是它被评过的证据；
    # "评得够不够认真"是语义判断，归 reviewer。
    scored: set[str] = {m.group(1).strip().upper()
                        for m in re.finditer(r'"label"\s*:\s*"([^"]+)"', body)}
    scored |= {m.group(1).upper()
               for m in re.finditer(r"\b([QH]\d+)\b", body, re.IGNORECASE)}

    missing = [qid for qid in sorted(questions) if qid.upper() not in scored]
    if not missing:
        return {"applicable": True, "passed": True, "missing": [],
                "reason": f"{len(questions)} 个研究问题都有价值/可信性评估"}
    return {
        "applicable": True, "passed": False, "missing": missing,
        "reason": ("以下研究问题没有价值/可信性评估：" + ", ".join(missing)
                   + f"（已评：{', '.join(sorted(scored)) or '无'}）。"
                     "`score_hypothesis_innovation` 的 label 用问题编号；"
                     "纯探索题只填冗余/可信性/影响力，机制与预测维度可留空。"),
    }
