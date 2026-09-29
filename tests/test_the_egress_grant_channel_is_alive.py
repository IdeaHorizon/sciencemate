"""出网授权这条通道，端到端到底通不通（#1068）。

P1 的形状是：**通道看起来在工作，实际上一次都没生效过**。四处各自独立地断着：

1. `effective_egress_policy` 调 `capability_grants.granted_values` /
   `CAPABILITY_EGRESS_DOMAIN` —— 这两个符号在那个模块里**不存在**（第一版接口，
   09-15 合并时留下的），异常被一个宽 `except` 吞掉，生效授权集合恒为空；
2. `grant_from_answer` 在生产代码里**零调用方** —— 人点了"允许"也没落成授权；
3. 确认卡的选项是 `{label, description}` 对象：平台 `options: list[str]` 校验失败
   → 会话详情 **500**，前端 `map(String)` 之后点任一选项发出 `"[object Object]"`，
   CLI 把 dict 原样印出来；
4. 自主档下这张卡被自动作答，而回给模型的是一段与事实不符的话（"你没带
   recommended_option_index"——可这张卡带着，`auto_approve_answer` 只是没读它）。

任何一处没修，人点的那下都到不了墙上。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core import capability_grants as cg
from core.bootstrap import bootstrap
from core.pause import PauseEvent
from core.pause_driver import auto_approve_answer
from core.sandbox import effective_egress_policy
from core.state import State


def _state() -> State:
    bootstrap()
    return State.new(node_type="experiment", base_dir=Path(tempfile.mkdtemp()), project_id=None)


# ── 1. 生效白名单真的把授权算进去 ────────────────────────────────────────


def test_an_approved_host_reaches_the_effective_policy(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "pypi.org")
    state = _state()

    assert "psl.noaa.gov" not in effective_egress_policy(state)["entries"]
    assert cg.grant_from_answer(state, "https://psl.noaa.gov/data/x.nc", "允许") is True

    policy = effective_egress_policy(state)
    assert "psl.noaa.gov" in policy["entries"], (
        "人批了，生效白名单一动不动 —— 这就是 #1068：通道看着在工作，一次都没生效过")
    assert policy["granted"] == ["psl.noaa.gov"]
    # 验收 5：部署基线与 run 级授权必须分得开
    assert policy["from_environment"] == ["pypi.org"]
    assert "grant" in policy["source"]


def test_without_a_state_there_is_no_grant_set(monkeypatch) -> None:
    """不传 state 问的是另一个问题（部署基线是什么），授权恒空 —— 但不许报错。"""
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "pypi.org")
    policy = effective_egress_policy()
    assert policy["granted"] == []
    assert policy["entries"] == ["pypi.org"]


def test_an_interface_mismatch_is_not_swallowed_into_an_empty_set(monkeypatch) -> None:
    """拿到 state 却读不出授权 = 接口对不上，得当场炸，不能记成"没人批过"。"""
    state = _state()

    def _boom(_state):
        raise AttributeError("granted_values 不存在（第一版接口）")

    monkeypatch.setattr(cg, "granted_hosts", _boom)
    with pytest.raises(AttributeError):
        effective_egress_policy(state)


# ── 2. 人的答复落成授权（resume 路径上真的调了） ─────────────────────────


def test_the_resume_path_settles_a_capability_grant() -> None:
    """`drive_pause_chain` 里必须有 capability_grant 的结算分支。

    没有它，`grant_from_answer` 就是一个没有调用方的函数 —— 确认卡照常弹、人照常
    点、`is_granted` 仍然为假，模型再申请就再弹一张。
    """
    import inspect

    from core import pause_driver

    src = inspect.getsource(pause_driver.drive_pause_chain)
    assert "capability_grant" in src and "grant_from_answer" in src, (
        "resume 路径不认这个暂停类型 —— 人点的那下停在界面上")


@pytest.mark.parametrize("answer,expected", [
    ("允许", True),
    ("1", True),
    ("[1]", True),
    ("allow", True),
    ({"choice_id": "allow"}, True),
    ("拒绝", False),
    ("2", False),
    ({"choice_id": "deny"}, False),
    ("允许 —— 本次 run 内准许连接 psl.noaa.gov", True),
])
def test_every_way_a_person_can_answer_is_understood(answer, expected) -> None:
    """界面印什么、人就会答什么：裸序号、选项原文、结构化 choice 都得认。

    实测挂过的那些写法（`1` / `2` / `[object Object]`）全在这里。
    """
    assert cg.read_answer(answer) is expected


def test_an_unreadable_answer_is_not_a_refusal() -> None:
    """读不出来 ≠ 拒绝。后者是人做了决定，前者是这条链某处坏了。"""
    assert cg.read_answer("[object Object]") is None
    assert cg.read_answer("") is None
    state = _state()
    assert cg.grant_from_answer(state, "psl.noaa.gov", "[object Object]") is False


# ── 3. 确认卡的选项是纯字符串 ────────────────────────────────────────────


def test_the_card_options_are_plain_strings() -> None:
    """平台 `PendingApprovalOut.options: list[str]`：dict 会让会话详情 500。"""
    state = _state()
    card = cg.request_network_grant(state, "https://psl.noaa.gov/x", "取再分析数据")
    options = card["pause_event"]["options"]
    assert options and all(isinstance(o, str) for o in options), options
    assert options[0].startswith("允许") and options[1].startswith("拒绝")
    assert card["pause_event"]["recommended_option_index"] == 1


# ── 4. 自主档：按推荐项拒绝，而不是回一段与事实不符的话 ────────────────


def test_unattended_mode_actually_refuses_instead_of_lecturing() -> None:
    state = _state()
    card = cg.request_network_grant(state, "psl.noaa.gov", "取数据")
    event = PauseEvent.from_payload(card["pause_event"])

    answer = auto_approve_answer(event)
    assert answer == "2", (
        "自主档回的不是「拒绝」那一项，而是那段「你没带 recommended_option_index」"
        f"的话 —— 而这张卡带着推荐项。实际回了：{answer!r}")
    assert cg.read_answer(answer) is False


# ── 5. 主机解析与来路三分 ────────────────────────────────────────────────


def test_the_card_asks_about_the_host_the_request_will_really_reach() -> None:
    """`?@` / `#@` 之后的东西不是主机 —— 卡上问的必须是 curl 真正要连的那个。"""
    assert cg.normalize_host("https://evil.example?@psl.noaa.gov/data/x.nc") == "evil.example"
    assert cg.normalize_host("https://evil.example#@psl.noaa.gov/") == "evil.example"
    assert cg.normalize_host("https://user:pw@data.example.org:8443/x") == "data.example.org"
    assert cg.normalize_host("HTTPS://Data.Example.ORG./x") == "data.example.org"


def test_a_redirect_origin_has_three_cases_not_two(monkeypatch) -> None:
    """本 run 已授权 / 部署白名单放行 / 都不是 —— 「换源」只留给第三种。"""
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "huggingface.co")
    state = _state()

    from_allowlist = cg.request_network_grant(
        state, "us.aws.cdn.hf.co", "取权重", redirect_of="huggingface.co",
    )["pause_event"]["context"]
    assert "部署白名单" in from_allowlist
    assert "换源" not in from_allowlist, (
        "部署白名单早就放行的来路被定性成「在换源」—— 和来路不明一字不差")

    unknown = cg.request_network_grant(
        state, "us.aws.cdn.hf.co", "取权重", redirect_of="evil.example",
    )["pause_event"]["context"]
    assert "换源" in unknown

    cg.grant_from_answer(state, "huggingface.co", "允许")
    granted = cg.request_network_grant(
        state, "us.aws.cdn.hf.co", "取权重", redirect_of="huggingface.co",
    )["pause_event"]["context"]
    assert "本次已获授权" in granted and "换源" not in granted
