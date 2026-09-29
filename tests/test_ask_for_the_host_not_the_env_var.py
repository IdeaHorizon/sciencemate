"""撞到出网白名单时要能问人，而不是只打印一句「去改环境变量重启」（#770）。

现状：内置白名单五个域（pypi / files.pythonhosted / github / githubusercontent /
zenodo）**全是装软件用的，没有一个科学数据源**。于是每个真实数据任务都撞墙，
而撞墙后节点只能建议人去改 `HARNESS_SANDBOX_EGRESS_ALLOWLIST` 再重启重跑。
节点侧的测试还专门钉死「不许悄悄写进 os.environ」—— 那是对的，agent 能改自己的墙
就没有墙。缺的不是放松，是**一条通道**。

这组判据钉三件事：
1. 通道真的存在、而且是**注册过的工具**（不是只有个函数躺在那）；
2. 授权按 run 计、逐个主机、不落盘、不进环境；
3. **没答就是没批** —— 问过和批过是两件事。
"""
from __future__ import annotations

import os

import pytest

from core.capability_grants import (
    grant_from_answer,
    granted_hosts,
    grants_ledger,
    is_granted,
    normalize_host,
    request_network_grant,
)


class _State:
    node_type = "experiment"
    run_id = "r-1"


def test_the_tool_is_actually_registered():
    """文案许诺了能力，API 就得给得出 —— 模型点名一个不存在的工具会跳过整道流程。"""
    import shared.tools.builtin  # noqa: F401  （注册副作用）
    from core.tool_registry import get_tool

    assert get_tool("request_network_access") is not None, (
        "工具没注册进去 —— 模型读到描述却调不到，等于这条通道不存在")


def test_asking_goes_through_the_existing_pause_channel():
    out = request_network_grant(_State(), "https://psl.noaa.gov/data/x.nc",
                                "取 NOAA 的再分析数据", what_for="air.mon.mean.nc")
    assert out["status"] == "pause", "没走 HITL 通道就没人会看见这个请求"
    ev = out["pause_event"]
    assert ev["metadata"]["type"] == "capability_grant"
    assert ev["metadata"]["host"] == "psl.noaa.gov"
    assert "psl.noaa.gov" in ev["question"]
    assert "取 NOAA 的再分析数据" in ev["context"], "人要靠这句判断，不能丢"
    assert "本次 run" in ev["context"], "必须写明批准的作用域，否则人不知道自己批了什么"


def test_default_recommendation_is_to_refuse():
    """无人值守会按推荐项自动作答 —— 推荐必须是拒绝，不能替人放行。

    2026-09-21（#1068）：选项从 `{label, description}` 换成**纯字符串**。那两个
    dict 一路把这条链堵死 —— 平台 `PendingApprovalOut.options: list[str]` 校验失败
    让会话详情 500、前端 `map(String)` 之后点任一选项发出 `"[object Object]"`、
    CLI 把 dict 原样印出来。断言跟着换形状，**这条判据本身一个字没变**：
    推荐项必须是拒绝。
    """
    ev = request_network_grant(_State(), "example.org", "x")["pause_event"]
    recommended = ev["options"][ev["recommended_option_index"]]
    assert isinstance(recommended, str), "选项不是纯字符串，平台会 500"
    assert recommended.startswith("拒绝")


@pytest.mark.parametrize("answer", ["允许", "allow", "Yes", " 同意 "])
def test_a_yes_grants_this_host(answer):
    st = _State()
    assert grant_from_answer(st, "psl.noaa.gov", answer, reason="NOAA") is True
    assert is_granted(st, "psl.noaa.gov")
    assert is_granted(st, "https://psl.noaa.gov/data/x.nc"), "给整条 URL 也要认得出"


@pytest.mark.parametrize("answer", [None, "", "拒绝", "no", "等一下我再想想"])
def test_no_answer_is_not_a_grant(answer):
    """没答、答不清楚、明确拒绝 —— 都不批。问过和批过是两件事。"""
    st = _State()
    assert grant_from_answer(st, "psl.noaa.gov", answer) is False
    assert not is_granted(st, "psl.noaa.gov")


def test_a_grant_does_not_cover_siblings_or_parents():
    """批一个主机不等于批它的父域或兄弟 —— 否则一次点击的含义没边。"""
    st = _State()
    grant_from_answer(st, "data.example.org", "允许")
    assert is_granted(st, "data.example.org")
    assert not is_granted(st, "evil.example.org")
    assert not is_granted(st, "example.org")


def test_a_grant_never_touches_the_environment():
    """agent 能改自己的墙就没有墙 —— 节点侧的测试也钉着这一条。"""
    before = dict(os.environ)
    st = _State()
    grant_from_answer(st, "psl.noaa.gov", "允许", reason="NOAA")
    assert os.environ == before, "授权把自己写进了环境变量"
    assert "HARNESS_SANDBOX_EGRESS_ALLOWLIST" not in os.environ


def test_grants_do_not_leak_between_runs():
    """按 run 计：另一个 state 看不见这次的授权。"""
    a, b = _State(), _State()
    grant_from_answer(a, "psl.noaa.gov", "允许")
    assert is_granted(a, "psl.noaa.gov")
    assert not is_granted(b, "psl.noaa.gov")


def test_the_ledger_says_who_asked_and_why():
    st = _State()
    grant_from_answer(st, "psl.noaa.gov", "允许", reason="取 NOAA 再分析数据")
    (row,) = grants_ledger(st)
    assert row["host"] == "psl.noaa.gov"
    assert row["reason"] == "取 NOAA 再分析数据"
    assert row["asked_by_node_type"] == "experiment"
    assert granted_hosts(st) == ("psl.noaa.gov",)


@pytest.mark.parametrize("raw,expected", [
    ("HTTPS://PSL.NOAA.GOV:443/x", "psl.noaa.gov"),
    ("user:pw@data.example.org/a/b", "data.example.org"),
    ("psl.noaa.gov.", "psl.noaa.gov"),
    # IPv6 不带方括号：`urlsplit().hostname` 就是这么给的，而判据现在**以它为准**
    # （#1068 补充三第 3 条：手拼那套认不出 `?@`/`#@`，卡上问的主机和请求真正要连的
    # 不是同一个）。两边比较都走 normalize_host，所以口径一致。
    ("[2001:db8::1]:8080", "2001:db8::1"),
])
def test_host_normalisation(raw, expected):
    assert normalize_host(raw) == expected
