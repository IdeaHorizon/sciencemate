"""一句两种语言并排写的话，按语言说出来。

单独一个、不 import 任何东西的模块：`self_update` 在启动器里、配置读出来之前就被
import，它要说话就不能去拉用户模型那一层。别处从 `user_interface` 拿它（同一个函数）。

两种语言写在调用点（同前端 `useT` 的理由）：分开放的话，改了一边忘了另一边不报错。
`tests/test_every_error_speaks_both_languages.py` 扫盘守着：报错里的中文必须写在一个
同时有 zh 和 en 的 dict 里；说给人听的异常（`SaidToAPerson`）构造时必须带 `en=`。
"""

from __future__ import annotations

from collections.abc import Mapping


def say(phrase: Mapping[str, str], lang: str, **fields: object) -> str:
    """`{name}` 由 fields 填；没有这种语言就说中文那句（不该发生 —— 扫盘闸要两种都写）。"""
    text = phrase.get(lang) or phrase["zh"]
    return text.format(**fields) if fields else text


def in_both_languages(phrase: Mapping[str, str], **fields: object) -> str:
    """还没有界面、也就不知道该说哪种语言的时候：两种都说，中文一行、英文一行。

    启动器、数据根配错这类话在任何一屏出现之前就说了，读它的是原生壳的对话框或终端。
    猜一种语言等于对一半的人说外语，所以并排写。
    """
    return say(phrase, "zh", **fields) + "\n" + say(phrase, "en", **fields)


class SaidToAPerson(Exception):
    """一句会原样说给某个人听的报错 —— 两种语言都写。

    `str()` 是中文那句（日志、老调用方、测试的 `match=` 都读它）；`en` 必填、只收关键字 ——
    漏写英文就是一个 TypeError，而不是英文界面上冒出一句中文（2026-10-19：服务层的报错
    经 `detail=str(exc)` 原话透到界面上，英文那一屏照样是中文）。说给哪一屏听用 `said_in(lang)`。

    凡是消息会到人眼前的异常（HTTP 响应、接口返回的状态字段、界面上显示的错误事件、
    桌面壳的对话框）都继承它。扫盘闸按"定义了 `said_in` 的类及其子类"找，不列类名：
    每一处构造都得带 `en=`；透传处不许 `str(exc)` —— 这种用 `said_in(lang)`，别人的异常
    原话只当证据（`say(…, why=exc)` / `said_of`）。
    """

    def __init__(self, message: str, *, en: str) -> None:
        super().__init__(message)
        self.en = en

    def said_in(self, lang: str) -> str:
        return self.en if lang == "en" else str(self)


def said_of(error: BaseException, lang: str, *, otherwise: Mapping[str, str]) -> str:
    """一个异常说给这一屏听。

    它自己两种语言都写了（`SaidToAPerson`）就照它说；不是的（第三方库的、只有一句技术
    原话的），原话只当证据填进 `otherwise` 那句两种语言的话里的 `{why}`。用在一个
    `except` 同时接得住两种的地方 —— 比如 `except ValueError`，或者一个大部分子类是
    英文技术原话、个别子类是说给人听的基类。
    """
    # 框架那边（`core.materials.MaterialError` 之类）不 import 平台，但长着同一个形状 ——
    # 两种语言都写了的，就照它自己的说。
    said_in = getattr(error, "said_in", None)
    if isinstance(error, SaidToAPerson) or callable(said_in):
        return error.said_in(lang)  # type: ignore[attr-defined]
    return say(otherwise, lang, why=error)
