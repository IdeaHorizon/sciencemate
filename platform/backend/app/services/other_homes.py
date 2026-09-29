"""别处的家 —— 本机之外，项目还能住在哪。

## 核心只认一个家

一个项目只有一个家：本机，或者某个别处（`project_homes`）。核心自己只知道
「本机」这一种；能把项目安在别处的**提供者**由发行在装配时登记（专业版登记
「组织」）。一台没登记任何提供者的机器上，这里的每个函数都是直通：清单只有
本机的，指名要住到别处的项目建不成、且说得出为什么。

这层存在的理由是边界：侧栏那份按家分组的清单、新建项目时挑家，都是核心的
事；「组织」是什么、怎么问它，不是。核心不 import 任何一个提供者的模块。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ThereIsNoSuchHomeError(Exception):
    """建不成，且**说得出为什么** —— 界面要把这句话原样显示。"""

    def __init__(self, message: str, status: int = 404) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class HomeProvider:
    """一种「别处」。三个函数都在调用那一刻才解析（登记的是 lambda），
    这样测试 monkeypatch 提供者模块里的函数照样生效。"""

    kind: str
    #: 这个家 id 是不是它认识的（新建项目时按 `home` 找提供者）。
    knows: Callable[[str], bool]
    #: 住在它那儿、这个人看得见的项目 —— 每一行带 `home`。
    list_projects: Callable[[], Awaitable[list[dict]]]
    #: 在那个家里建一个项目；不认识 / 连不上 / 被拒都抛 `ThereIsNoSuchHomeError`。
    create_project: Callable[[str, dict], Awaitable[dict]]


_PROVIDERS: list[HomeProvider] = []


def at_home_here() -> dict:
    """本机项目的家。"""
    return {"kind": "local", "reachable": True}


def offer(provider: HomeProvider) -> None:
    """登记一种别处的家。装配时调一次。"""
    _PROVIDERS.append(provider)


def the_providers() -> tuple[HomeProvider, ...]:
    return tuple(_PROVIDERS)


def withdraw_everything() -> None:
    """测试用：拆掉全部登记。"""
    _PROVIDERS.clear()


async def the_ones_that_live_elsewhere() -> list[dict]:
    """每一种别处的家里、这个人看得见的项目。并行问 —— 几种家不该排队等几次超时。
    没有提供者就是空：一台没连过任何组织的桌面上，这个函数和从前逐字一样。"""
    if not _PROVIDERS:
        return []
    gathered = await asyncio.gather(*(p.list_projects() for p in _PROVIDERS), return_exceptions=True)
    everything: list[dict] = []
    for provider, answer in zip(_PROVIDERS, gathered, strict=True):
        if isinstance(answer, BaseException):
            logger.warning("问「%s」这种家时抛了：%s", provider.kind, answer)
            continue
        everything.extend(answer)
    return everything


async def create_it_elsewhere(home: str, payload: dict) -> dict:
    """在指名的那个家里建项目。没有提供者认识它 → 说清楚。"""
    for provider in _PROVIDERS:
        if provider.knows(home):
            return await provider.create_project(home, payload)
    raise ThereIsNoSuchHomeError("没有这个家 —— 它可能已经被退出了，或者这个版本装不了别处的家")
