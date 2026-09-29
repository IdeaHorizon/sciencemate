"""答复必须对着**这一次呈递**判 —— 2026-08-19 死循环的回归。

现场：curator 没整合完那轮，框架把 PROCEED 撤下了菜单（合法选项是
`[retry_curator, revise, redirect_upstream, abort, edit]`），而 UI 上印的却是
另一套选项集的文案 `PROCEED to next stage`。人点了三次：

    _parse_choice("PROCEED to next stage", [retry_curator, …]) → None
      → 一行 transcript → entry 一个字不动 → 下游仍被拦 → 原地重呈递

三次点击，两条 `decision_answer_unparsed`，零反馈。

这里钉死三件事：
  1. 不在选项集里的答复 → **Rejection 带合法出口**，不是静默 None
  2. 裸序号与屏幕同源取项 —— 不可能"屏幕 PROCEED、账本 retry_curator"
  3. 对着上一次呈递的答复 → 拒收，不沿用
"""
from core.decision_offer import Answer, Choice, Offer, Rejection, resolve_answer

CURATOR_PENDING = (
    Choice("retry_curator", "RETRY CURATOR INTEGRATION (re-run _curator integration)"),
    Choice("revise", "REVISE (re-run source_node with reviewer feedback)"),
    Choice("redirect_upstream", "REDIRECT to upstream"),
    Choice("abort", "ABORT pipeline"),
    Choice("edit", "EDIT manually then proceed"),
)
NORMAL = (
    Choice("proceed", "PROCEED to next stage"),
    Choice("revise", "REVISE (re-run source_node with reviewer feedback)"),
    Choice("redirect_upstream", "REDIRECT to upstream"),
    Choice("abort", "ABORT pipeline"),
    Choice("edit", "EDIT manually then proceed"),
)


def _offer(choices, recommended):
    return Offer(decision_id="decision_run_x_1787106513-ec6ea0", kind="post_node",
                 question="Post-node decision for hypothesis",
                 choices=choices, recommended_id=recommended)


def test_answer_outside_the_action_set_is_rejected_with_a_way_out():
    """事故原样：菜单没有 PROCEED，答复却是 PROCEED 的文案。"""
    out = resolve_answer(_offer(CURATOR_PENDING, "retry_curator"), "PROCEED to next stage")

    assert isinstance(out, Rejection), "认不出必须吵，不能返回 None 让授权凭空消失"
    # 关键：报错里带合法出口。旧路径连报错都没有，人只能再点一次。
    assert "retry_curator" in out.legal_choice_ids
    assert "proceed" not in out.legal_choice_ids


def test_bare_index_reads_from_the_same_list_the_human_saw():
    """人输 "1"：取的必须是**这次呈递**的第一项，不是另一套动作表的第 0 项。

    旧 `_parse_choice` 按 `entry["decision_options"]` 取索引，而屏幕上的文案来自
    另一次构造 —— 于是"屏幕写 PROCEED、账本记 retry_curator"，两边都不报错。
    """
    offer = _offer(CURATOR_PENDING, "retry_curator")
    out = resolve_answer(offer, "1")

    assert isinstance(out, Answer)
    assert out.choice_id == "retry_curator"
    # 同源：序号取到的那一项，label 就是屏幕上印的那一行。
    assert offer.choice(out.choice_id).label.startswith("RETRY CURATOR")


def test_no_substring_matching_between_different_action_sets():
    """`edit` 的 label 里含 "proceed"，但 proceed 不在这次的选项集里。

    子串匹配正是让 "PROCEED to next stage" 去撞 `proceed` 的那条路 —— 它在选项集
    变化时会安静地匹配到另一个动作。这里确认它已经不存在。
    """
    out = resolve_answer(_offer(CURATOR_PENDING, "retry_curator"), "proceed")
    assert isinstance(out, Rejection)
    assert out.code == "unrecognized_answer"


def test_answer_to_a_superseded_offer_is_refused_not_reused():
    """curator 修好后选项集从 retry_curator 换成 proceed —— 那是**另一次呈递**。"""
    stale = _offer(CURATOR_PENDING, "retry_curator")
    current = _offer(NORMAL, "proceed")
    assert stale.offer_id != current.offer_id

    out = resolve_answer(current, {"offer_id": stale.offer_id, "choice_id": "proceed"})
    assert isinstance(out, Rejection)
    assert out.code == "offer_superseded"


def test_structured_answer_is_a_set_membership_test_not_a_guess():
    out = resolve_answer(_offer(NORMAL, "proceed"),
                         {"offer_id": _offer(NORMAL, "proceed").offer_id,
                          "choice_id": "proceed", "note": "证据够了"})
    assert isinstance(out, Answer)
    assert (out.choice_id, out.note) == ("proceed", "证据够了")


def test_offer_survives_the_round_trip_through_a_pause_payload():
    """答复是对着从 payload 还原的 offer 判的 —— 还原必须逐字一致。"""
    offer = _offer(CURATOR_PENDING, "retry_curator")
    restored = Offer.from_pause_payload(offer.to_pause_payload())

    assert restored.offer_id == offer.offer_id
    assert restored.choice_ids() == offer.choice_ids()
    # 顺序也得一致，否则裸序号会取到另一项。
    assert [c.label for c in restored.choices] == [c.label for c in offer.choices]


def test_auto_approve_still_resolves_after_the_parser_got_strict():
    """严格化不能把无人值守卡住 —— 自动作答走的是序号路径。

    `resolve_answer` 比它取代的 `_parse_choice` 严格得多（不再子串匹配）。严格化
    最容易误伤的就是**没有人在场重答**的那条路：auto-approve / 连续模式。

    auto_approve_answer 返回的是 `str(recommended_index + 1)`，而 recommended_index
    来自 pause metadata、与 option_details 同源 —— 所以序号取到的正是屏幕上高亮的
    那一项。这条以前不成立（索引取自另一套 actions 表），现在成立。
    """
    from core.pause import PauseEvent
    from core.pause_driver import auto_approve_answer

    offer = _offer(CURATOR_PENDING, "retry_curator")
    payload = offer.to_pause_payload()
    pause = PauseEvent.from_payload({
        **payload,
        "metadata": {
            "type": "decision_package",
            "recommended_option_index": payload["recommended_option_index"],
        },
    })

    auto = auto_approve_answer(pause)
    assert auto == "1"

    out = resolve_answer(Offer.from_pause_payload(pause.to_dict()), auto)
    assert isinstance(out, Answer), "无人值守的自动作答必须仍被受理"
    # 自动选中的，就是框架推荐的那一项 —— 不是"另一套表的第 0 项"。
    assert out.choice_id == offer.recommended_id == "retry_curator"


def test_free_text_without_a_choice_id_is_refused_rather_than_guessed():
    """人手打一句话（CLI 里常见）→ 重问，而不是蒙一个动作执行掉。

    旧路径会把 "就按推荐的来吧" 里的任何一个动作词子串蒙中；蒙不中则静默丢弃。
    两种结局都不该有：认不出就说认不出，并把合法集给出去。
    """
    out = resolve_answer(_offer(CURATOR_PENDING, "retry_curator"), "就按推荐的来吧")
    assert isinstance(out, Rejection)
    assert set(out.legal_choice_ids) == {
        "retry_curator", "revise", "redirect_upstream", "abort", "edit"}
