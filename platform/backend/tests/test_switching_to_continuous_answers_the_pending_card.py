"""切成连续档的那一刻，**已经挂在屏幕上**的决策卡要被替人点掉（推荐项）。

## 现场（2026-08-24，英国饮食会话 014ed0ad）

人对着 post_node 决策卡把档位切成「连续」。broadcast_autonomy 把新档位推到了
worker（以后的决策点不再停），可**这一张**卡还在 —— worker 攥着 pause 在等一次
answer 派发，开关翻了它也不会自己走。对用户来说就是"连续模式不生效"。

## 边界（与 harness 侧同源）

- 只在连续档（"*" 预授权）触发 —— 与 `apply_autonomy` 里 AUTO_APPROVE 的判据
  同一个，不另造一档。
- 只答 `waiting_human`。`waiting_permission`（高危审批）不碰：回头把一条已经
  拍在人脸上的高危命令自动批掉，是拿两个各自无害的机制组合出销毁。
- 只答呈递方自己声明过推荐的；推荐指向不存在的选项 = 呈递不一致，留给人。
- 作答走 `execute_local_turn`，与人点按钮同一条路。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.services import local_execution


def _binding() -> SimpleNamespace:
    return SimpleNamespace(run_id="run_x", user_id="user_x", session_id="sess_x")


class _FakeDB:
    """最小 DB 环境：被测单元是 `_answer_with_recommendation` 自己的取舍逻辑。"""

    def __init__(self, *, run_status: str, decision) -> None:
        self._objects = {
            "Run": SimpleNamespace(status=run_status),
            "User": SimpleNamespace(id="user_x"),
            "SessionProjection": SimpleNamespace(session_id="sess_x"),
            "Project": SimpleNamespace(id="proj_x"),
        }
        self._decision = decision

    async def get(self, cls, _key):
        return self._objects.get(cls.__name__)

    async def scalar(self, _stmt):
        return self._decision

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _patch_env(monkeypatch, db, *, still_paused: bool = True):
    calls: list[dict] = []

    async def _capture_turn(_db, **kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr("app.database.get_session_factory", lambda: lambda: db)
    monkeypatch.setattr(local_execution, "execute_local_turn", _capture_turn)
    monkeypatch.setattr(
        local_execution,
        "harness_session_manager",
        SimpleNamespace(
            paused_binding=lambda *_a: (_binding() if still_paused else None),
        ),
    )
    return calls


@pytest.mark.asyncio
async def test_the_pending_recommended_card_gets_answered(monkeypatch) -> None:
    decision = SimpleNamespace(
        recommended_choice_id="proceed",
        choices=[
            {"choiceId": "proceed", "label": "Continue"},
            {"choiceId": "abort", "label": "Stop research"},
        ],
        project_id="proj_x",
        context={"offerId": "1788917217-bfe3f6:p6e729f0e:o41e10000"},
    )
    calls = _patch_env(monkeypatch, _FakeDB(run_status="waiting_human", decision=decision))

    await local_execution._answer_with_recommendation(_binding())

    assert len(calls) == 1, "停在 waiting_human 的推荐项没有被替人点掉"
    assert calls[0]["message"] == "Continue", "答复文本必须与人点按钮时一字不差（选项 label）"
    assert calls[0]["choice"] == {
        "offer_id": "1788917217-bfe3f6:p6e729f0e:o41e10000", "choice_id": "proceed",
    }, "替人点的也必须指回这一次呈递 —— offer_id=None 会落在下一张卡上"


@pytest.mark.asyncio
async def test_a_card_without_a_presentation_identity_is_not_answered_for_the_user(
    monkeypatch,
) -> None:
    """没有 offer_id 的决策行（老 transcript）不替人答：答不准是哪一张。

    2026-09-09 node20：自动答复带着 `offer_id=None` 发出，运行时无从核对，于是
    它落在了人没看见的下一张卡上；人随后点的那张被判 offer_superseded。
    """
    decision = SimpleNamespace(
        recommended_choice_id="proceed",
        choices=[{"choiceId": "proceed", "label": "Continue"}],
        project_id="proj_x",
        context={},
    )
    calls = _patch_env(monkeypatch, _FakeDB(run_status="waiting_human", decision=decision))

    await local_execution._answer_with_recommendation(_binding())

    assert calls == []


@pytest.mark.asyncio
async def test_a_permission_pause_is_never_auto_approved(monkeypatch) -> None:
    """高危审批停在 waiting_permission 上 —— 切档不许替人批。"""
    decision = SimpleNamespace(
        recommended_choice_id="approve",
        choices=[{"choiceId": "approve", "label": "批准执行"}],
        project_id="proj_x",
        context={"offerId": "run:p:o"},
    )
    calls = _patch_env(monkeypatch, _FakeDB(run_status="waiting_permission", decision=decision))

    await local_execution._answer_with_recommendation(_binding())

    assert calls == [], "waiting_permission 被切档自动批掉了 —— 高危审批只能人来"


@pytest.mark.asyncio
async def test_no_recommendation_means_no_guess(monkeypatch) -> None:
    decision = SimpleNamespace(
        recommended_choice_id=None,
        choices=[{"choiceId": "proceed", "label": "Continue"}],
        project_id="proj_x",
        context={"offerId": "run:p:o"},
    )
    calls = _patch_env(monkeypatch, _FakeDB(run_status="waiting_human", decision=decision))

    await local_execution._answer_with_recommendation(_binding())

    assert calls == [], "没有推荐项的问题被框架猜了一个答案"


@pytest.mark.asyncio
async def test_a_recommendation_outside_the_choice_set_is_left_to_the_human(monkeypatch) -> None:
    decision = SimpleNamespace(
        recommended_choice_id="ghost",
        choices=[{"choiceId": "proceed", "label": "Continue"}],
        project_id="proj_x",
        context={"offerId": "run:p:o"},
    )
    calls = _patch_env(monkeypatch, _FakeDB(run_status="waiting_human", decision=decision))

    await local_execution._answer_with_recommendation(_binding())

    assert calls == [], "推荐指向不存在的选项还被照答 —— 呈递不一致时必须留给人"


@pytest.mark.asyncio
async def test_it_rechecks_the_pause_at_the_last_moment(monkeypatch) -> None:
    """出手前 pause 已经没了（人先点了）→ 不发。

    没有这道复核，晚到的自动答复会被当成**新一轮**的用户消息 —— 推荐项的
    label（"Continue"）变成一条凭空的研究指令。
    """
    decision = SimpleNamespace(
        recommended_choice_id="proceed",
        choices=[{"choiceId": "proceed", "label": "Continue"}],
        project_id="proj_x",
        context={"offerId": "run:p:o"},
    )
    calls = _patch_env(
        monkeypatch,
        _FakeDB(run_status="waiting_human", decision=decision),
        still_paused=False,
    )

    await local_execution._answer_with_recommendation(_binding())

    assert calls == [], "pause 已被人答掉，自动答复还是发出去了 —— 它会变成新一轮"


def test_the_config_endpoint_triggers_it_only_for_continuous() -> None:
    """接线：PATCH config 升到连续档才触发，判据与 harness 的 AUTO_APPROVE 同源。"""
    from app.api.v1 import projects as projects_api

    source = inspect.getsource(projects_api.update_project_config)
    assert "answer_pending_decisions_with_recommendation" in source, (
        "切档没有接到\"替人点掉已挂着的推荐项\" —— broadcast 只管以后的决策点，"
        "停着的那张卡会继续等人"
    )
    gate = source.index('policy.mode != "assisted"')
    call = source.index("answer_pending_decisions_with_recommendation(")
    assert gate < call, "触发必须被连续档判据（\"*\" 预授权）罩住，自主/协作档不许替人点"
