"""决策包不许静默丢内容 —— 丢了必须自报丢了多少。

2026-09-01 本机 E2E 实拍：hypothesis 节点写完四条假说的终态，人在决策包里
看到的正文是

    **四条假说/研究问题的终态**：

后面一片空白，直接跳到下一段。四条裁决——正是这次要人拍板的东西——被
`producing_summary.strip().splitlines()[:8]` 切掉了，而呈给人的文本里没有
任何迹象表明它被切过。同一份包里 `artifact_ids_produced[:10]` 藏掉了第 11
个产物，concern 描述被 `[:200]` 切在半句话中间（"若 S4 实际包含 ensemble 统"）。

有界是对的（决策包不能无限长），静默是错的：人得知道自己看的是不是全部。
"""
from __future__ import annotations

import re

from core.decision_offer import Choice, Offer
from shared.tools.library.decision_package import _render_decision_package


def _offer() -> Offer:
    return Offer(
        decision_id="1788243242-6a5597:p054c5bb4",
        kind="decision_package",
        question="Post-node decision for hypothesis",
        choices=(Choice(id="proceed", label="PROCEED to next stage"),
                 Choice(id="revise", label="REVISE")),
        recommended_id="proceed",
    )


def _render(**kw) -> str:
    base = dict(
        source_node_type="hypothesis",
        producing_run_id="1788243242-6a5597",
        producing_summary="",
        artifact_ids_produced=[],
        curator_summary="",
        review_critique_json=None,
        review_failed_reason=None,
        offer=_offer(),
        recommended_feedback="",
    )
    base.update(kw)
    return _render_decision_package(**base)


# 生产实拍的形状：前 8 行是标题+空行+裁决对象，第 8 行正好是「终态」这个
# 标题，四条裁决在它后面。
PRODUCTION_SUMMARY = "\n".join([
    "本轮 revise 裁决已完成。总结如下：",
    "",
    "## 本轮裁决（revise 第 7 轮）",
    "",
    "**裁决对象**：experiment 交付的写作前置闸 #1 + 闸 #3，review 与 curator 均通过。",
    "",
    "**四条假说/研究问题的终态**：",
    "",
    "- Q1: supported（零基线 0.03 vs 假阳组 0.94–1.00）",
    "- Q2: supported（配平效应量后相图成立）",
    "- Q3: inconclusive（机制在非线性下不复现）",
    "- Q4: not_run（可辨识性闸未执行，写成局限）",
])


def test_the_four_verdicts_reach_the_human() -> None:
    """人要拍板的四条终态必须真的出现在决策包里。"""
    text = _render(producing_summary=PRODUCTION_SUMMARY)
    for verdict in ("Q1: supported", "Q2: supported",
                    "Q3: inconclusive", "Q4: not_run"):
        assert verdict in text, f"{verdict!r} 没有送到人眼前：\n{text}"


def test_a_summary_too_long_to_show_says_how_much_it_hid() -> None:
    """真的超长时可以截断，但必须报出丢了几行。"""
    summary = "\n".join(f"第 {i} 行" for i in range(1, 201))
    text = _render(producing_summary=summary)
    assert "第 1 行" in text
    m = re.search(r"另有 (\d+) 行未显示", text)
    assert m, f"截断了却没自报：\n{text[-800:]}"
    shown = sum(1 for i in range(1, 201) if f"第 {i} 行" in text)
    assert shown + int(m.group(1)) == 200, (
        f"自报的丢弃量对不上：显示 {shown} 行，自报丢 {m.group(1)} 行，共 200 行")


def test_hidden_artifacts_are_counted() -> None:
    """产物列表卡在上限时，人得知道还有几个没列出来。"""
    ids = [f"survey_report__a{i:03d}" for i in range(40)]
    text = _render(artifact_ids_produced=ids)
    m = re.search(r"另有 (\d+) 个产物未列出", text)
    assert m, f"藏了产物却没自报：\n{text[:1500]}"
    shown = sum(1 for a in ids if a in text)
    assert shown + int(m.group(1)) == 40


def test_a_concern_is_not_cut_mid_sentence_without_saying_so() -> None:
    """concern 是人决定 revise/proceed 的依据，切了必须说切了。"""
    long_desc = ("literature 节点因 web_fetch 上限被截断在 SI S3.1 Eq.52，"
                 "S4 全文未读到。" + "补充说明。" * 300)
    critique = {"verdict": "approve", "confidence": 0.88,
                "concerns": [{"severity": "minor", "description": long_desc}]}
    text = _render(review_critique_json=critique)
    assert "此处截断，全文" in text, f"concern 被切了却没自报：\n{text}"


def test_nothing_is_marked_truncated_when_nothing_was_dropped() -> None:
    """没丢东西就不许瞎报 —— 否则自报标记本身失去信号价值。"""
    text = _render(
        producing_summary="一行总结。",
        artifact_ids_produced=["survey_report__x"],
        review_critique_json={"verdict": "approve", "confidence": 0.9,
                              "concerns": [{"severity": "minor", "description": "短"}]},
    )
    assert "未显示" not in text
    assert "未列出" not in text
    assert "此处截断" not in text
