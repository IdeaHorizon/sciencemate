"""档位的环境变量叫什么名字 —— 文档里写的那个，必须就是代码读的那个。

2026-09-05 写验收脚本时实测：RFC、派工单、README 里一律写
`PLATFORM_PROFILE=org`，而代码只认裸 `PROFILE`。后果不是"配置不生效"这么中
性 —— 一台按文档配好的组织服务器会以**个人档**起来：隐式本机用户、零鉴权、
谁连上来都是"本人"。配错往开放的方向倒，而且一声不吭。

这类缺陷有个共同长相：一件事有两份手写的答案（这里是"变量叫什么"），两份都
合法、没有任何一层能发现它们分叉了。判据因此落在**同一个名字**上。
"""
from __future__ import annotations

import pytest

from app.config import Settings


@pytest.mark.parametrize("name", ["PLATFORM_PROFILE", "PROFILE"])
def test_the_documented_env_name_actually_switches_the_profile(monkeypatch, name: str) -> None:
    monkeypatch.delenv("PLATFORM_PROFILE", raising=False)
    monkeypatch.delenv("PROFILE", raising=False)
    monkeypatch.setenv(name, "org")
    assert Settings(_env_file=None).profile == "org"


def test_nothing_in_the_environment_means_personal(monkeypatch) -> None:
    """什么都不设 = 个人电脑上的一份安装。这是"零配置"那句话的兜底。"""
    monkeypatch.delenv("PLATFORM_PROFILE", raising=False)
    monkeypatch.delenv("PROFILE", raising=False)
    assert Settings(_env_file=None).profile == "personal"


def test_an_unknown_profile_refuses_to_start(monkeypatch) -> None:
    """写错档位名要当场炸，不能悄悄回落成个人档。

    「回落成默认」在这里等于「回落成没有鉴权」—— 一个打错的字不该有这个
    量级的后果。
    """
    monkeypatch.setenv("PLATFORM_PROFILE", "orgnization")
    with pytest.raises(Exception):
        Settings(_env_file=None)


def test_callers_still_construct_it_by_field_name() -> None:
    """别名是给环境变量的，不是给调用方的。

    `Settings(profile=...)` 在测试里有六处 —— 加别名时顺手把它们废掉，红的会是
    那六处，而指不到"我给字段加了个别名"这个原因。
    """
    assert Settings(profile="org", _env_file=None).profile == "org"
    assert Settings(profile="personal", _env_file=None).profile == "personal"
