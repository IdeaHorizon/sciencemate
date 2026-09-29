"""这一次呈递必须**整份**活着走完全程 —— 从 harness 到平台事件。

## 为什么要有这条

`test_decision_answer_contract.py` 测的是 `resolve_answer(offer, ...)`，前提是
Offer 已经完好。它测不到"Offer 在传输链上被削掉一半"，所以 2026-08-19 这个
事故单测全绿：

    harness  Offer.to_pause_payload()  →  option_details 带 id、offer_id、facts
    平台 ingest 手写投影                →  只抄 {label, description, recommended}
    前端                                →  拿不到 id，只能按位次兜底
    答复                                →  撞不上合法动作集 → 静默丢弃 → 原地重呈递

人点三次没反应，四层没有任何一层报错。

## 这条钉死的不变量

**中间层不许决定呈递的哪些字段能活下来。** 判据不是"有没有抄全"（那要人去列
清单，清单会过期），而是：呈递从管子这头进去，从那头出来还能**还原成同一个
offer_id**。少一个字段就还原不出来，测试就红。
"""
from core.decision_offer import PAUSE_OFFER_KEY, Choice, Offer

OFFER = Offer(
    decision_id="run_x_1787155777-702875:pdeadbeef",
    kind="post_node",
    question="Post-node decision for hypothesis",
    choices=(
        Choice("retry_reviewer", "RETRY REVIEWER (re-run _reviewer only)", "只重跑审查"),
        Choice("revise", "REVISE (re-run source_node with reviewer feedback)", "带反馈重跑"),
        Choice("abort", "ABORT pipeline", "终止"),
    ),
    context="NODE COMPLETED: hypothesis",
    recommended_id="retry_reviewer",
    facts={"reviewFailed": True, "producingRunId": "1787155777-702875"},
)


def test_offer_survives_harness_to_pause_event():
    """发射端：pause_event 必须带着整份呈递，不是它的摘要。"""
    payload = OFFER.to_pause_payload()
    pause_event = {**payload, PAUSE_OFFER_KEY: payload, "context": "完整决策包正文"}

    restored = Offer.from_pause_payload(pause_event[PAUSE_OFFER_KEY])
    assert restored.offer_id == OFFER.offer_id, "还原不出同一次呈递 —— 路上被削过"
    assert restored.choice_ids() == OFFER.choice_ids()
    assert restored.facts == OFFER.facts, "facts 是「为什么问你」，不能在路上丢"


def test_every_choice_keeps_its_id_through_the_projection():
    """选项的身份不许在任何一跳消失 —— 它是答复能落地的唯一凭据。"""
    details = OFFER.to_pause_payload()["option_details"]
    assert [d["id"] for d in details] == ["retry_reviewer", "revise", "abort"]
    assert all(d.get("id") for d in details), "有选项丢了 id"


def test_kind_travels_or_the_self_check_silently_degrades():
    """`kind` 参与 offer_id 派生。漏了它，接收端永远重算不出同一个 id。

    这正是上一版发射端逐字段手抄时漏掉的那个 —— 漏了不报错，只是自校验一路
    在失败、一路走降级路径。
    """
    payload = OFFER.to_pause_payload()
    assert payload["kind"] == "post_node"

    crippled = {k: v for k, v in payload.items() if k != "kind"}
    try:
        Offer.from_pause_payload(crippled)
    except Exception as exc:  # OfferContractError
        assert "offer_id" in str(exc), "少了 kind 应当吵 offer_id 对不上"
    else:
        raise AssertionError("少了 kind 却还原成功了 —— 自校验没起作用")


def test_ingest_does_not_rebuild_options_by_hand():
    """机械判据：ingest 的 pause 分支里不许出现选项字段名。

    这条比"检查有没有抄全"强：抄全是一次性的，而只要"按名字列举"这个动作还在，
    上游下次加字段照样静默掉。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "platform/backend/app/services/execution_ingest.py"
    text = src.read_text(encoding="utf-8")
    start = text.index('if raw_event in {"run_paused", "loop_pause"}:')
    end = text.index('if raw_event == "loop_resume":', start)
    branch = text[start:end]
    # 注释里出现是可以的（那是在解释为什么不再这么干），代码里不行。
    code = "\n".join(
        line for line in branch.splitlines() if not line.strip().startswith("#")
    )
    for field in ('"label"', '"description"', '"recommended"'):
        assert field not in code, (
            f"ingest 的 pause 分支又开始逐字段重建选项了（{field}）—— "
            "呈递要整份搬运，不要在这里挑字段"
        )


def test_wire_key_matches_on_both_sides_of_the_process_boundary():
    """backend 是独立进程不 import harness 包，线协议键在两侧各写一次字面量 ——
    这条把"各写一次"钉成"必须相等"，改任何一侧不改另一侧就红。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in ("platform/backend/app/services/execution_ingest.py",
                "platform/backend/app/services/sessions.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert 'PAUSE_OFFER_KEY = "offer"' in text, f"{rel} 的线协议键与 harness 不一致"
        assert "from core.decision_offer import" not in text, f"{rel} 不许 import harness 包"
    assert PAUSE_OFFER_KEY == "offer"
