"""会话邮箱：话怎么进来、怎么被消费、以及每条的终局（RFC D12 + P1-2）。

这个文件接替 `test_interrupt_inbox.py`。被接替的那套验的是**文件投递**
（写目录、rename、轮询、坏文件挪走…），而那整条投递面已经退休 ——
它当初存在的理由是"协议不能多路复用"，那件事在 P1-1 修好了。

保留下来的是**语义**，一条不少：顺序、有界、空话拒收、超长截断、
message_id 全程带着、停止机械不过模型、停止作废排队的话。
新增的是 D12 要求的两件：每条消息必有终局，以及毒丸熔断在消费路径上。

## 2026-08-24：停止搬出了这个队列

停止曾经是队列里的一条 `KIND_STOP`。队列必须有人取，而消费者的寿命绑在
"某一种操作"上 —— `answer` 那条路一个都没有，于是自主档的停止按钮**完全
失效**（node20 会话 a6f156e4：19 次点击、19 条 received、0 条 consumed）。
现在停止在分发点同步施加，不入队。这个文件因此分成两半：队列只管话，
停止的行为验在 `stop_now` / 分发点上，判据是「不管有没有消费者都生效」。
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from core import session_inbox
from core.session_inbox import KIND_MESSAGE, Inbox, make


def _msg(text: str = "换个思路", **kwargs):
    return make(KIND_MESSAGE, text, **kwargs)


# ── 队列语义（原文件里那些，去掉文件系统的部分）──────────────────────────────

def test_order_is_delivery_order() -> None:
    """连按几次回车时顺序不能乱 —— 后一句常常是对前一句的补充。"""
    inbox = Inbox()
    for text in ("先别用 CNKI", "补充：优先英文库", "再补一句"):
        inbox.put(_msg(text))
    seen = []
    while (item := inbox.take()) is not None:
        seen.append(item.text)
        inbox.consumed(item)
    assert seen == ["先别用 CNKI", "补充：优先英文库", "再补一句"]


def test_empty_text_is_rejected_at_the_boundary() -> None:
    """空话不许进 —— 否则 agent 会为一句空白跑一个决策轮，白烧钱。"""
    with pytest.raises(ValueError):
        _msg("   ")


def test_overlong_text_is_truncated_not_rejected() -> None:
    """误粘一整个文件时截断，而不是拒收：人的本意通常在开头。"""
    item = _msg("x" * (session_inbox.MAX_TEXT_CHARS + 500))
    assert len(item.text) < session_inbox.MAX_TEXT_CHARS + 100
    assert item.text.startswith("x" * 100)
    assert "截断" in item.text


def test_stop_is_not_a_queue_item_at_all() -> None:
    """停止不再是一种可入队的东西 —— 这是"信号不进消息队列"的机械判据。

    它曾经是 `KIND_STOP`。造得出这条条目，就意味着它要等人来取，而"谁来取"
    的答案是一份会变长的操作名单 —— 那正是 08-24 停止失效的出处。
    """
    assert session_inbox.KINDS == (KIND_MESSAGE,)
    assert not hasattr(session_inbox, "KIND_STOP")
    with pytest.raises(ValueError, match="unknown inbox kind"):
        make("stop", "停止当前轮")


def test_unknown_kind_is_rejected_at_the_boundary() -> None:
    """拼错的 kind 在造它的那一刻就炸，不是被静默当成 message。"""
    with pytest.raises(ValueError, match="unknown inbox kind"):
        make("stopp", "停")


def test_message_id_is_carried_all_the_way() -> None:
    """App Server 落了消息行才投递，worker 的回执和答复靠它锚回那条消息。
    丢了它，答复只能挂进 run 的活动窗口，渲染在提问上面（2026-08-18 实测）。"""
    inbox = Inbox()
    inbox.put(_msg("跑的怎么样了？", message_id="msg-80112a58"))
    assert inbox.take().message_id == "msg-80112a58"


# ── 每条消息必有终局（D12）──────────────────────────────────────────────────

def test_draining_pending_clears_their_poison_counters_too() -> None:
    """条目离开队列，它的毒丸计数也得走 —— 留着就是一个没有主人的账。

    `drain_pending` 现在有两个调用点（轮结束收尾、施加停止时作废排队的话），
    两处都是"这些条目从此与本队列无关"。
    """
    inbox = Inbox(poison_threshold=2)
    item = _msg("先别用 CNKI")
    inbox.put(item)
    assert inbox.failed(item) is False          # 记了一次
    assert [i.text for i in inbox.drain_pending()] == ["先别用 CNKI"]
    inbox.put(item)                              # 同一条又进来
    assert inbox.failed(item) is False, "计数没跟着条目一起走，第二次就被误判成毒丸"


def test_a_consumed_item_is_gone() -> None:
    """插话是一次性指令，不是要重放的证据（证据在 transcript 里）。"""
    inbox = Inbox()
    item = _msg()
    inbox.put(item)
    inbox.consumed(item)
    assert inbox.take() is None


# ── 毒丸熔断在消费路径上（D12）──────────────────────────────────────────────

def test_a_message_that_keeps_crashing_is_quarantined() -> None:
    """毒丸只能隔离不能预防 —— 但它必须**被**隔离，不能无限重拉。

    熔断的计数由邮箱自己维护，不经过任何外部账本。PR#553 的教训：熔断器挂在
    账本上，而失败那条路绕过了账本记账，于是熔断器"存在但不在场"。
    """
    inbox = Inbox(poison_threshold=2)
    item = _msg("这句话会让消费方崩")
    inbox.put(item)
    assert inbox.failed(item) is False, "第一次崩可能是偶发，还该再试一次"
    assert inbox.take() is item, "还没到阈值就不许把它丢掉"
    assert inbox.failed(item) is True, "到阈值就该隔离"
    assert inbox.take() is None
    assert [i.item_id for i in inbox.quarantined()] == [item.item_id]


def test_a_healthy_message_after_a_poison_pill_still_gets_through() -> None:
    """隔离的是**那一条**，不是整个邮箱 —— 否则一句坏话能让会话哑掉。"""
    inbox = Inbox(poison_threshold=1)
    bad, good = _msg("毒丸"), _msg("正常的话")
    inbox.put(bad)
    inbox.put(good)
    assert inbox.failed(bad) is True
    assert inbox.take() is good


def test_the_failure_count_is_per_item_not_global() -> None:
    """两条各崩一次 ≠ 一条崩两次。计数按 item_id，否则第二条会被前一条连坐。"""
    inbox = Inbox(poison_threshold=2)
    first, second = _msg("一"), _msg("二")
    inbox.put(first)
    inbox.put(second)
    assert inbox.failed(first) is False
    assert inbox.failed(second) is False
    assert len(inbox) == 2


# ── 停止：到达即生效，不依赖任何消费者（platform_runtime）───────────────────


def _stop_probe(tmp_path: Path, *, operation_active: bool = True):
    """一个刚好够 `stop_now` 用的会话替身 —— 方法全绑**真的**那几个。

    绑真方法而不是塞假的：手工替身一旦和真对象分叉，测的就是替身
    （[[替身遮住被测实现]]）。
    """
    import platform_runtime
    from core.state import State

    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    emitted: list[dict] = []
    session = types.SimpleNamespace(
        state=state,
        request_id="req-1",
        _inbox=Inbox(),
        _operation_active=operation_active,
        _wake=None,
        # 第一位是帧类型（"progress"），字段里另有 event="user.stop" ——
        # 参数名不能叫 event，会跟字段撞。
        emit=lambda frame, **fields: emitted.append({"frame": frame, **fields}),
    )
    for name in ("_wake_signal", "stop_now"):
        setattr(session, name, types.MethodType(
            getattr(platform_runtime.PlatformSession, name), session))
    return session, state, emitted


def _transcript(state) -> list[dict]:
    return [json.loads(line) for line
            in state.transcript_path.read_text(encoding="utf-8").splitlines() if line]


def test_stop_takes_effect_with_no_consumer_running(tmp_path: Path) -> None:
    """**08-24 事故的回放**：没有任何收件箱消费者时，停止照样生效。

    当天的现场：无人值守跑到高危点 paused → 平台走 `answer` 答复并续跑，
    而 `answer` 这条路从来没有开过消费者。用户点 19 次，19 条
    `inbox_item_received`、**0 条 consumed**，停止后 12 分钟 agent 还在调工具。

    所以判据不是"消费者取走之后停止会生效"，而是**根本不问有没有消费者**：
    这个测试全程没有事件循环、没有 drain 任务、没有跑轮 —— 停止仍须落地。
    """
    session, state, emitted = _stop_probe(tmp_path)

    occupancy = session.stop_now(author="u1")

    assert state.hook_state["kill_signal"]["requested_by"] == "user_stop:u1"
    assert occupancy == "working"
    assert [e["frame"] for e in emitted] == ["progress"]
    assert emitted[0]["event"] == "user.stop"
    received = [r for r in _transcript(state) if r.get("event") == "user_stop_received"]
    assert received and received[0]["author"] == "u1"


def test_the_stop_op_applies_at_the_dispatch_point(tmp_path: Path) -> None:
    """走**真入口**（`_handle_management_op`）：op=stop 进门就生效，不入队。

    ⚠️ 这条是本文件里唯一咬得住"接线"的测试，别删。其余几条直接调
    `stop_now`，验的是那个方法自己对不对；而 08-24 的 bug 根本不在方法里，
    在**入口把 stop 变成了一条队列条目**。变异实测：把这一行接线换成
    `occupancy = "working"`（假装收下、什么也不做），只调 stop_now 的那些
    测试全绿 —— 正是当天生产的形状（后端 200、agent 照跑）。
    """
    import platform_runtime

    session, state, _ = _stop_probe(tmp_path)
    session._authorized_risk_classes = []
    accepted: list[dict] = []
    session.emit = lambda frame, **f: accepted.append({"frame": frame, **f})

    platform_runtime._handle_management_op(
        session, "stop", {"author": "u1"}, "req-stop-1")

    assert state.hook_state["kill_signal"]["requested_by"] == "user_stop:u1", (
        "op=stop 走完真入口之后 kill_signal 没落地 —— 接线断了，"
        "而后端仍会如实回 200「已送达」"
    )
    assert len(session._inbox) == 0, "停止又变回队列条目了 —— 它就会重新需要一个消费者"
    ack = [e for e in accepted if e["frame"] == "accepted"]
    assert ack and ack[0]["item_id"] == "", "停止没有队列条目，回执不该编一个 item_id"


def test_the_interject_op_still_goes_through_the_queue(tmp_path: Path) -> None:
    """同一个入口的另一半：话仍然入队（它需要一轮真实待命轮才能回答）。

    与上一条成对 —— 只删停止那条分支，别把插话也一起"简化"掉。
    """
    import platform_runtime

    session, state, _ = _stop_probe(tmp_path)
    session._authorized_risk_classes = []
    accepted: list[dict] = []
    session.emit = lambda frame, **f: accepted.append({"frame": frame, **f})
    session.receive = types.MethodType(
        platform_runtime.PlatformSession.receive, session)

    platform_runtime._handle_management_op(
        session, "interject", {"author": "u1", "text": "先别用 CNKI"}, "req-say-1")

    assert len(session._inbox) == 1
    assert session._inbox.take().text == "先别用 CNKI"
    assert "kill_signal" not in state.hook_state, "插话不该顺手把这一轮停了"


def test_stop_never_asks_the_model(tmp_path: Path) -> None:
    """停止是一个布尔：直接写 kill_signal，不跑待命轮（不烧模型、不会判错）。

    判据落在"它够不够得着模型"上：替身连 harness/client 都没有，一旦有人
    把停止改回走 `_handle_drained_interrupt`（那条路要 `chat._handle_interrupt`），
    这里就会 AttributeError 而不是悄悄多烧一轮。
    """
    session, state, _ = _stop_probe(tmp_path)
    assert not hasattr(session, "client") and not hasattr(session, "harness")

    session.stop_now(author="u1")

    assert state.hook_state["kill_signal"]
    assert not any(r.get("event") == "user_interrupt_received"
                   for r in _transcript(state))


def test_stop_supersedes_the_words_queued_for_this_turn(tmp_path: Path) -> None:
    """那些话是说给"这一轮"听的，而这一轮马上就没了。

    作废本身是旧语义（原来由 `Inbox.put` 在收下 stop 时做）；停止搬出队列
    之后，它由**施加停止的那一处**做 —— 同一件事，发生在它真正发生的地方。
    每条都必须说出口（终局 = superseded），不能就那么躺着。
    """
    session, state, _ = _stop_probe(tmp_path)
    session._inbox.put(_msg("先别用 CNKI"))
    session._inbox.put(_msg("补充：优先英文库"))

    session.stop_now(author="u1")

    assert len(session._inbox) == 0
    gone = [r for r in _transcript(state) if r.get("event") == "inbox_item_superseded"]
    assert len(gone) == 2
    assert {r["reason"] for r in gone} == {"stop_cancels_queued_messages"}


def test_stop_wakes_a_parked_loop(tmp_path: Path) -> None:
    """停靠中的无人值守循环必须被叫醒 —— 否则时延变成"等到下一个复查点"。

    `next_action` 第一问就是"continuous 还在跑吗"，而停止刚把它关掉；不叫醒
    它，那个答案要几小时后才被问到（退避可以退到 4 小时）。
    """
    import asyncio

    async def go():
        session, _state, _ = _stop_probe(tmp_path)
        session._wake_signal().clear()
        session.stop_now(author="u1")
        return session._wake_signal().is_set()

    assert asyncio.run(go()) is True


# ── 消费者：一份实现，嵌套时只有最外层负责 ──────────────────────────────────


@pytest.mark.asyncio
async def test_exactly_one_inbox_consumer_when_nested(tmp_path: Path) -> None:
    """`run_unattended` 套着 `turn` 时，收件箱只能有一个消费者。

    这曾经是两份手写的记账，而 `run_unattended` 那份**忘了加计数** —— 于是
    内层每个 turn 都自认最外层，两个消费者抢同一个收件箱（正是那段注释声称
    要避免的"同一条话被处理两次"）。两份抄件只改对一份，分叉时两边都不报错。
    """
    import platform_runtime

    started = 0

    async def _fake_drain(self) -> None:
        nonlocal started
        started += 1
        await asyncio.Event().wait()          # 活着但不做事

    import asyncio

    drained = 0

    async def _fake_consume(self) -> bool:
        # 收尾排空（"每条消息必有终局"）也走这个 stub —— 桩要满足被测方法的
        # 完整契约，否则测的是一个现实里不存在的对象。
        nonlocal drained
        drained += 1
        return False

    session = types.SimpleNamespace(
        _inbox_consumers=0,
        state=types.SimpleNamespace(append_transcript=lambda *a, **k: None),
    )
    session._drain_interrupts_forever = types.MethodType(_fake_drain, session)
    session.consume_inbox = types.MethodType(_fake_consume, session)
    ctx = types.MethodType(platform_runtime.PlatformSession._inbox_consumer, session)

    async with ctx():                          # 外层（run_unattended）
        await asyncio.sleep(0)
        assert started == 1
        async with ctx():                      # 内层（turn）
            await asyncio.sleep(0)
            assert started == 1, "内层又开了一个 —— 两个消费者抢同一个收件箱"
        assert session._inbox_consumers == 1, "内层退出把外层的账也销了"
    assert session._inbox_consumers == 0
    # 最外层退出时必须再排空一次：只 cancel 不排空，排队的话就凭空消失
    # （2026-08-31 实测 received=5 / consumed=2 / superseded=0）。
    assert drained == 1, "最外层收尾没有排空邮箱"

