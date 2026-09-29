"""request_human_input 的结构化契约（台账 #3）。

事故：pause 的 metadata 是空的，options 是 agent 自由撰写的四条字符串，
其中 options[0] = "A: 等平台修复合约门"。autonomous 的兜底是"选第一个"——
于是无人值守模式**自己选择了停摆**。

新契约同时解决两件事：description 让人在 UI 上能判断后果；
recommended_option_index 让无人值守有安全答案。同一个字段两头都用上。
"""
from __future__ import annotations

import asyncio

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute


@pytest.fixture(autouse=True)
def _boot():
    bootstrap(force=True)


def _state(tmp_path):
    return State.new(node_type="experiment", base_dir=tmp_path / "runs")


def _ask(state, **kw):
    return asyncio.run(execute("request_human_input", state, **kw))


_GOOD = [
    {"label": "用 v13 预注册重跑", "description": "指名 prereg_artifact_id，约 25 分钟"},
    {"label": "降级为诊断运行", "description": "结果不能用于确证，但能定位合约门"},
]


def test_options_without_recommendation_are_rejected_with_an_exit(tmp_path) -> None:
    r = _ask(_state(tmp_path), question="怎么办？", options=_GOOD)
    assert r["status"] == "error"
    # 报错必须给合法出口，不能只说"不许"
    assert "recommended_option_index" in r["error"]
    assert "report_blocker" in r["error"]


def test_recommended_index_must_be_in_range(tmp_path) -> None:
    r = _ask(_state(tmp_path), question="q", options=_GOOD, recommended_option_index=7)
    assert r["status"] == "error" and "越界" in r["error"]


def test_a_waiting_looking_recommendation_is_the_models_call(tmp_path) -> None:
    """判决拆除第三波（builtin:1000 删）：曾有一张「等待/wait/hold」关键词表替模型判
    推荐项算不算推进 —— 硬编码审美阈值，「wait for job then analyze」会误伤。
    推荐什么是判断，判断归模型；框架只把推荐项如实呈递。墙加回去这条转红。"""
    opts = [
        {"label": "等作业跑完再分析", "description": "wait for the job, then analyze"},
        {"label": "手动跑两个模拟", "description": "日志放回 runtime/"},
    ]
    r = _ask(_state(tmp_path), question="q", options=opts, recommended_option_index=0)
    assert r["status"] == "pause", r
    assert r["pause_event"]["recommended_option_index"] == 0


def test_pause_event_carries_details_and_recommendation(tmp_path) -> None:
    r = _ask(_state(tmp_path), question="q", context="c", header="采样方案",
             options=_GOOD, recommended_option_index=1)
    pe = r["pause_event"]
    assert pe["option_details"][1]["description"]
    # 选项有身份：答复回传 id，不回传文案。
    assert [d["id"] for d in pe["option_details"]] == ["option_1", "option_2"]
    assert pe["offer_id"] and pe["decision_id"] and pe["kind"] == "structured_question"
    # recommended 是**呈递的**事实，在 pause_event 顶层（由 Offer 投影摊开），
    # 不再在 metadata 里另存一份。
    assert pe["recommended_option_index"] == 1
    assert pe["recommended_choice_id"] == "option_2"
    assert pe["metadata"]["type"] == "structured_question"
    assert pe["header"] == "采样方案"
    # 兼容既有消费方：options 仍是字符串数组
    assert pe["options"] == [o["label"] for o in _GOOD]


def test_autonomous_picks_the_recommended_option() -> None:
    """用**真的** PauseEvent，不用手工造的替身。

    这里原本是个 `class _PE` 桩，只有 metadata / options 两个属性。桩不会跟着
    真类演化：`PauseEvent` 后来长出 `payload` 和 `recommended_index()`，桩上没
    有，于是这条测试测的是一个现实中不存在的形状。
    """
    from core import pause_driver
    from core.pause import PauseEvent

    pe = PauseEvent(
        question="q",
        options=["用 v13 重跑", "降级诊断"],
        metadata={"type": "structured_question"},
        payload={"recommended_option_index": 1},
    )
    assert pause_driver.auto_approve_answer(pe) == "2"   # 1-indexed


def test_autonomous_reads_recommendation_from_the_offer_not_metadata() -> None:
    """推荐项是呈递的事实。老 pause 把它放在 metadata —— 兜底仍认。"""
    from core import pause_driver
    from core.pause import PauseEvent

    legacy = PauseEvent(
        question="q",
        options=["A", "B", "C"],
        metadata={"type": "structured_question", "recommended_option_index": 2},
        payload={},
    )
    assert pause_driver.auto_approve_answer(legacy) == "3"


def test_autonomous_never_guesses_without_a_recommendation() -> None:
    """没带推荐项时不猜 —— 选任意一个都可能正好是让研究停摆的那个。

    先前试过"跳过看起来像等待的选项"，但那是关键词名单：「先等等」就漏了。
    名单式护栏必然漏新写法，而这里是承重判据，漏一次就是一次卡死。
    """
    from core import pause_driver

    from core.pause import PauseEvent

    for options in (["等平台修复合约门", "手动跑两个模拟"],
                    ["先等等", "暂停观察"],
                    ["方案甲", "方案乙"]):
        answer = pause_driver.auto_approve_answer(
            PauseEvent(question="q", options=list(options), metadata={}, payload={})
        )
        assert answer not in options, f"不许从 {options} 里挑一个"
        assert "recommended_option_index" in answer and "report_blocker" in answer


def test_plain_string_options_still_work(tmp_path) -> None:
    """历史 harness 传字符串数组 —— 兼容，但仍要求推荐项。"""
    r = _ask(_state(tmp_path), question="q", options=["跑 A", "跑 B"],
             recommended_option_index=0)
    assert r["status"] == "pause"
    assert r["pause_event"]["option_details"][0]["label"] == "跑 A"
    assert r["pause_event"]["option_details"][0]["id"] == "option_1"
