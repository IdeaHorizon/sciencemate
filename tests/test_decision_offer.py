"""Offer 是选项集的唯一来源 —— 三个投影不许分叉，答复不许静默丢失。

主判据不是"这些函数返回对不对"，是**防复发**：让选项集分叉这件事在结构上做不到，
让认不出的答复必须吵。2026-08-19 那次事故（人点三次 PROCEED、卡片原地重现三次）
里，四层比的都是文案，四层都没能发现 UI 上是一个已被撤下的选项。
"""
from __future__ import annotations

import pytest

from core.decision_offer import (
    Answer,
    Choice,
    Offer,
    OfferContractError,
    Rejection,
    resolve_answer,
)


def _curator_pending_offer() -> Offer:
    """复刻事故当时那次呈递：curator 没整合完 → 菜单里**没有** proceed。"""
    return Offer(
        decision_id="decision_run_x_1787106513-ec6ea0",
        kind="decision_package",
        question="Post-node decision for hypothesis",
        choices=(
            Choice("retry_curator", "RETRY CURATOR INTEGRATION", "重跑整合"),
            Choice("revise", "REVISE", "带反馈重跑 producer"),
            Choice("redirect_upstream", "REDIRECT to upstream", "退回上游"),
            Choice("abort", "ABORT pipeline", "终止"),
            Choice("edit", "EDIT manually", "人工编辑"),
        ),
        recommended_id="retry_curator",
    )


# ── 不变量：三个投影同源 ────────────────────────────────────────────────────


def test_three_projections_agree_on_the_choice_set():
    """正文、pause payload、账本合法集 —— 三份必须逐字对上同一个来源。"""
    offer = _curator_pending_offer()

    ledger_ids = offer.choice_ids()
    payload = offer.to_pause_payload()
    ascii_text = offer.to_ascii_options()

    payload_ids = tuple(d["id"] for d in payload["option_details"])
    assert payload_ids == ledger_ids

    # 兼容层的纯文案数组也必须派生自同一来源
    assert payload["options"] == [c.label for c in offer.choices]

    # 正文里出现的必须正好是这些 label，顺序一致
    for c in offer.choices:
        assert c.label in ascii_text
    assert ascii_text.index(offer.choices[0].label) < ascii_text.index(
        offer.choices[1].label
    )


def test_the_accident_shape_is_now_unrepresentable():
    """事故形态：UI 显示 PROCEED，账本合法集里却没有 proceed。

    以前这要两处各自构造才会发生；现在两者是同一个 `choices` 的投影，
    只要 proceed 不在集合里，它就不可能出现在任何一面上。
    """
    offer = _curator_pending_offer()
    payload = offer.to_pause_payload()

    assert "proceed" not in offer.choice_ids()
    assert not any(d["id"] == "proceed" for d in payload["option_details"])
    assert not any("PROCEED" in label for label in payload["options"])
    assert "PROCEED" not in offer.to_ascii_options()


def test_recommended_is_one_fact_not_two():
    """UI 高亮哪一项、框架推荐哪一项 —— 必须是同一个事实。

    事故里 UI 按 index 高亮了第 1 项（显示 PROCEED），框架推荐的是 retry_curator。
    """
    offer = _curator_pending_offer()
    payload = offer.to_pause_payload()

    idx = payload["recommended_option_index"]
    assert payload["option_details"][idx]["id"] == payload["recommended_choice_id"]
    assert payload["option_details"][idx]["recommended"] is True
    assert offer.to_ascii_options().count("← recommended") == 1


# ── 答复：不许静默丢失 ──────────────────────────────────────────────────────


def test_structured_answer_is_pure_membership():
    offer = _curator_pending_offer()
    got = resolve_answer(offer, {"offer_id": offer.offer_id, "choice_id": "retry_curator"})
    assert isinstance(got, Answer)
    assert got.choice_id == "retry_curator"


def test_the_actual_accident_answer_is_now_loud():
    """"PROCEED to next stage" 撞上没有 proceed 的菜单 —— 必须是吵的拒绝。

    事故当时：`_parse_choice` 返回 None → 一行 transcript → 授权凭空消失 →
    人再点一次 → 再消失一次。
    """
    offer = _curator_pending_offer()
    got = resolve_answer(offer, "PROCEED to next stage")

    assert isinstance(got, Rejection)
    assert got.code == "unrecognized_answer"
    # 合法出口必须随拒绝一起送达，别逼人猜
    assert got.legal_choice_ids == offer.choice_ids()
    assert "retry_curator" in got.message


def test_no_substring_matching():
    """子串匹配是事故的另一半 —— 它会安静地匹配到另一个动作。"""
    offer = _curator_pending_offer()
    # 含 "revise" 子串但不是它
    assert isinstance(resolve_answer(offer, "please do not revise anything"), Rejection)
    # 精确匹配才认
    assert isinstance(resolve_answer(offer, "revise"), Answer)


def test_index_answer_comes_from_the_same_order_as_the_render():
    """答 "1" 取到的必须是屏幕上第 1 项 —— 事故里这两者是错位的。"""
    offer = _curator_pending_offer()
    got = resolve_answer(offer, "1")
    assert isinstance(got, Answer)
    assert got.choice_id == offer.choices[0].id
    assert offer.choices[0].label in offer.to_ascii_options()


def test_answer_to_a_superseded_offer_is_rejected_not_reused():
    """重呈递后，对旧菜单作出的答复必须被拒绝并指向当前呈递。"""
    old = _curator_pending_offer()
    # curator 修好之后：同一个 decision，选项集变了
    new = Offer(
        decision_id=old.decision_id,
        kind="decision_package",
        question=old.question,
        choices=(
            Choice("proceed", "PROCEED to next stage"),
            *old.choices[1:],
        ),
        recommended_id="proceed",
    )
    assert new.offer_id != old.offer_id
    assert new.decision_id == old.decision_id

    got = resolve_answer(new, {"offer_id": old.offer_id, "choice_id": "retry_curator"})
    assert isinstance(got, Rejection)
    assert got.code == "offer_superseded"


def test_empty_and_out_of_range_are_rejections_with_the_legal_set():
    offer = _curator_pending_offer()
    for raw in ("", "  ", "9"):
        got = resolve_answer(offer, raw)
        assert isinstance(got, Rejection), raw
        assert got.legal_choice_ids == offer.choice_ids()


# ── 构造期就炸，别让坏 Offer 流到 UI ────────────────────────────────────────


def test_bad_offers_fail_at_construction():
    with pytest.raises(OfferContractError):
        Offer(decision_id="d", kind="k", question="q", choices=())

    with pytest.raises(OfferContractError):
        Offer(
            decision_id="d",
            kind="k",
            question="q",
            choices=(Choice("a", "A"), Choice("a", "A2")),
        )

    with pytest.raises(OfferContractError):
        Offer(
            decision_id="d",
            kind="k",
            question="q",
            choices=(Choice("a", "A"),),
            recommended_id="nope",
        )

    with pytest.raises(OfferContractError):
        Choice("Not A Stable Id", "label")

    with pytest.raises(OfferContractError):
        Choice("proceed", "")
