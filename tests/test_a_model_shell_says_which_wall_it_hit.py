"""模型 shell 撞上"永远没有外网"那堵墙时，得说清是哪堵墙（#997）。

活体：用户说"安装 WRF"，父编排器用 `run_bash` 做 curl / `git ls-remote` 预检，得到
DNS 解析正常但 TCP 443 `Permission denied`。它据此判定**这台机器没有外网**，反复要
用户上传源码或提供镜像 —— 而宿主同一时刻 `git ls-remote` 成功。它还 `find /home`
翻出了**别的项目**的旧 WRF 源码来复用。

它不是判断失误：返回值里没有任何东西能让它分辨「宿主断网」和「这条通道按设计无网」。
合法能力一直都在（`fetch_resource` 在一次性边界里开网、核验 hash 后原子导入），
只是没有任何一条规则说这件事该路由给谁，工具文案还写着它"适合 git"。

三处一起改：文案（说清永远没有外网）、返回值（结构化的下一步）、路由规则（落盘取
东西交给 experiment）。
"""
from __future__ import annotations

import pytest

from shared.tools.builtin import _network_refusal_hint


def test_a_blocked_acquisition_says_the_host_is_not_the_problem() -> None:
    hint = _network_refusal_hint(
        "curl -fsSL https://github.com/wrf-model/WRF/archive/v4.5.tar.gz -o wrf.tgz",
        "", "curl: (7) Failed to connect to github.com port 443: Permission denied")
    assert hint is not None
    assert hint["error_code"] == "network_unavailable_in_model_shell"
    assert hint["network_access"] is False
    assert hint["host_network_inferred"] is False, (
        "没有任何证据说明宿主断网 —— 模型上一次正是在这里拐错的")
    assert hint["recommended_node"] == "experiment"
    assert hint["recommended_tool"] == "fetch_resource"
    assert hint["safe_to_retry_same_tool"] is False
    assert "web_fetch" in hint["error"]


@pytest.mark.parametrize("cmd,err", [
    ("git ls-remote https://github.com/wrf-model/WRF", "Couldn't connect to server"),
    ("pip install numpy", "Network is unreachable"),
    ("wget https://example.org/x.tar.gz", "Connection refused"),
])
def test_the_usual_shapes_are_recognised(cmd, err) -> None:
    assert _network_refusal_hint(cmd, "", err) is not None


def test_a_local_failure_is_not_given_acquisition_advice() -> None:
    """只命中一个条件不加解释 —— 给本地 git 失败贴上"去起 experiment"更糟。"""
    assert _network_refusal_hint("git status", "", "not a git repository") is None
    assert _network_refusal_hint(
        "python train.py", "", "Permission denied") is None, (
        "命令根本不是在往外取东西，却被说成撞了网络墙")
    assert _network_refusal_hint(
        "curl -fsSL file:///tmp/x", "", "No such file or directory") is None


def test_the_tool_description_no_longer_says_it_suits_git() -> None:
    """契约文案与真实能力相矛盾，是这次误判的另一半。"""
    from core.bootstrap import bootstrap
    from core.tool_registry import get_tool

    bootstrap()
    desc = get_tool("run_bash").description
    assert "永远没有外网" in desc
    assert "fetch_resource" in desc and "web_fetch" in desc
    assert "本地 git" in desc, "笼统的「适合 git」要限定：本地可以，远端不可以"


def test_the_orchestrator_has_a_routing_rule_for_acquisition() -> None:
    """光有能力不够 —— 得有一条规则说这件事该交给谁。"""
    from core.loader import load_harness

    rules = "\n".join(load_harness("_orchestrator").rules)
    assert "fetch_resource" in rules
    assert "别自己 curl" in rules
    assert "web_fetch" in rules, "只读网页那条出口也要写明，否则会被一并推给 experiment"
