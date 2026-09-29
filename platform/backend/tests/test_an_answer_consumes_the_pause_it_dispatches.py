"""一个 pause 只能被答一次，答复必须指回它答的那一次呈递。

## 现场（2026-09-09 node20）

09:38:35 决策卡出现；0.4 秒后一条排队中的答复（切「连续」时替人点的推荐项，
`offer_id=None`）把它答掉，run 跑起来了。可平台侧 `paused` 要等**结果回来**
（09:47）才翻回 False，于是 09:43 人对着屏幕上那张旧卡点 PROCEED：入口的闸
（paused_binding）放行，在锁上排队，锁一空 pause 已换成下一张 —— 答复带着旧
offer 送到运行时被拒（offer_superseded），人看到空气泡加同一张卡。

修法不是名单：pause 在答复**派发**的那一刻就被消费；答复带 offer 而平台知道
活呈递是哪张时，不一致当场拒，不排队。
"""
from __future__ import annotations

import pytest

from app.services.harness_sessions import (
    AppRunBinding,
    HarnessAnswerSupersededError,
    HarnessSessionStaleError,
    _ProjectHarnessSession,
)


def _binding() -> AppRunBinding:
    return AppRunBinding("u", "c", "run_x", "c", "", None, "")


def _session(*, rpc_result: dict) -> tuple[_ProjectHarnessSession, list[dict]]:
    session = _ProjectHarnessSession(
        project_id="p",
        session_id="c",
        owner_user_id="u",
        backend_id="b",
        backend_fingerprint="fp",
        platform_context_hash=None,
        process=None,
        stderr_task=None,
        provider_secrets=(),
        channel=object(),  # type: ignore[arg-type]
        worker=object(),  # type: ignore[arg-type]
    )
    sent: list[dict] = []

    async def fake_rpc(payload: dict, **_: object) -> dict:
        sent.append(payload)
        # 派发的那一刻，pause 必须已经被消费
        assert session.paused is False and session.pause_id is None
        return {"data": rpc_result}

    session._rpc_locked = fake_rpc  # type: ignore[method-assign]
    session.binding = _binding()
    session.paused = True
    session.pause_id = "chatcmpl-tool-1"
    session.offer_id = "run1:p1:o1"
    return session, sent


async def _noop(_event: dict) -> None:
    return None


@pytest.mark.asyncio
async def test_dispatching_an_answer_consumes_the_pause_before_the_result_returns() -> None:
    session, sent = _session(rpc_result={"status": "completed"})
    await session.answer(
        binding=_binding(), answer="", choice={"offer_id": "run1:p1:o1", "choice_id": "proceed"},
        on_progress=_noop, on_protocol_event=_noop,
    )
    assert sent[0]["pause_id"] == "chatcmpl-tool-1", "派发的是它接到时的那个 pause"
    assert session.paused is False and session.offer_id is None


@pytest.mark.asyncio
async def test_a_second_answer_while_the_first_is_in_flight_finds_no_pause() -> None:
    session, _sent = _session(rpc_result={"status": "completed"})
    await session.answer(
        binding=_binding(), answer="", choice={"offer_id": "run1:p1:o1", "choice_id": "proceed"},
        on_progress=_noop, on_protocol_event=_noop,
    )
    with pytest.raises(HarnessSessionStaleError):
        await session.answer(
            binding=_binding(), answer="",
            choice={"offer_id": "run1:p1:o1", "choice_id": "proceed"},
            on_progress=_noop, on_protocol_event=_noop,
        )


@pytest.mark.asyncio
async def test_an_answer_for_an_earlier_presentation_is_refused_not_queued() -> None:
    session, sent = _session(rpc_result={"status": "completed"})
    with pytest.raises(HarnessAnswerSupersededError) as caught:
        await session.answer(
            binding=_binding(), answer="",
            choice={"offer_id": "run0:p0:o0", "choice_id": "proceed"},
            on_progress=_noop, on_protocol_event=_noop,
        )
    assert caught.value.answered == "run0:p0:o0" and caught.value.live == "run1:p1:o1"
    assert sent == [], "过期答复不许进队列"
    assert session.paused is True, "被拒的答复不消费 pause，人还能答当前那张"
    assert not isinstance(caught.value, HarnessSessionStaleError), (
        "过期呈递不是运行时丢了 —— 那个会被记账层翻成 stale_unknown"
    )


@pytest.mark.asyncio
async def test_the_next_pause_carries_its_own_presentation_identity() -> None:
    session, _sent = _session(rpc_result={
        "status": "paused",
        "pause_id": "chatcmpl-tool-2",
        "pause_event": {
            "pending_tool_call_id": "chatcmpl-tool-2",
            "question": "Post-node decision for observation",
            "offer": {"offer_id": "run2:p2:o2", "choices": []},
        },
    })
    await session.answer(
        binding=_binding(), answer="", choice={"offer_id": "run1:p1:o1", "choice_id": "proceed"},
        on_progress=_noop, on_protocol_event=_noop,
    )
    assert session.paused is True
    assert session.pause_id == "chatcmpl-tool-2"
    assert session.offer_id == "run2:p2:o2"


@pytest.mark.asyncio
async def test_an_answer_without_an_offer_is_still_accepted_for_old_presentations() -> None:
    """老前端 / 接回的 worker 报不出 offer 时不核对 —— 只在双方都报得出时才拒。"""
    session, sent = _session(rpc_result={"status": "completed"})
    session.offer_id = None
    await session.answer(
        binding=_binding(), answer="proceed", choice=None,
        on_progress=_noop, on_protocol_event=_noop,
    )
    assert sent and sent[0]["op"] == "answer"


@pytest.mark.asyncio
async def test_a_refused_dispatch_hands_the_pause_back() -> None:
    """worker 没收下（pause_id 对不上 / 传输炸了）= 没消费：pause 还回去，人还能答。"""
    from app.services.harness_sessions import HarnessSessionError

    session, _sent = _session(rpc_result={"status": "completed"})

    async def failing_rpc(payload: dict, **_: object) -> dict:
        raise HarnessSessionError("pause_id does not match")

    session._rpc_locked = failing_rpc  # type: ignore[method-assign]
    with pytest.raises(HarnessSessionError):
        await session.answer(
            binding=_binding(), answer="",
            choice={"offer_id": "run1:p1:o1", "choice_id": "proceed"},
            on_progress=_noop, on_protocol_event=_noop,
        )
    assert session.paused is True
    assert session.pause_id == "chatcmpl-tool-1" and session.offer_id == "run1:p1:o1"
