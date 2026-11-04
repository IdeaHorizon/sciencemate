"""一句两种语言并排写的话，按语言说出来。

单独一个、不 import 任何东西的模块：`self_update` 在启动器里、配置读出来之前就被
import，它要说话就不能去拉用户模型那一层。别处从 `user_interface` 拿它（同一个函数）。

两种语言写在调用点（同前端 `useT` 的理由）：分开放的话，改了一边忘了另一边不报错。
`tests/test_every_error_speaks_both_languages.py` 扫盘守着：报错里的中文必须写在一个
同时有 zh 和 en 的 dict 里；说给人听的异常（`SaidToAPerson`）构造时必须带 `en=`；
别人的异常原话（`{why}`）不拼进说给人听的话里（`said_of`）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

_log = logging.getLogger(__name__)


def say(phrase: Mapping[str, str], lang: str, **fields: object) -> str:
    """`{name}` 由 fields 填；没有这种语言就说中文那句（不该发生 —— 扫盘闸要两种都写）。"""
    text = phrase.get(lang) or phrase["zh"]
    return text.format(**fields) if fields else text


def in_either_language(phrase: Mapping[str, str], **fields: object) -> dict[str, str]:
    """写下来、以后才有人读的一句话：两种语言都说好，存成 `{"zh": …, "en": …}`。

    执行事件就是这样：摄取的时候没有人在看，日后打开它的可能是中文界面、也可能是英文
    界面（组织项目里还是不同的人）。读的那一刻用 `spoken` 挑一边。
    """
    return {lang: say(phrase, lang, **fields) for lang in ("zh", "en")}


def spoken(value: object, lang: str) -> object:
    """`in_either_language` 存下的一句，按这一屏的语言说出来；别的值原样返回。"""
    if (isinstance(value, Mapping) and set(value) == {"zh", "en"}
            and all(isinstance(text, str) for text in value.values())):
        return say(value, lang)
    return value


def readable_size(size_bytes: int) -> str:
    """字节数写成人读的大小（`104857600` → `100 MB`），报错里的大小上限用它。"""
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size_bytes >= scale:
            value = size_bytes / scale
            return f"{value:.0f} {unit}" if value >= 10 or value == int(value) else f"{value:.1f} {unit}"
    return f"{size_bytes} B"


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
    原话只当证据（`said_of` 记进日志，或者放进 `evidence`）。
    """

    def __init__(self, message: str, *, en: str, evidence: str = "") -> None:
        # `evidence` 是查问题用的原文（子进程 stderr、底层异常）：进 `str()`，于是进日志和
        # traceback；不进 `said_in` —— 那是说给人听的那一句，技术原文不往人眼前送。
        super().__init__(message + evidence)
        self.zh = message
        self.en = en
        self.evidence = evidence

    def said_in(self, lang: str) -> str:
        return self.en if lang == "en" else self.zh


def said_of(error: BaseException, lang: str, *, otherwise: Mapping[str, str]) -> str:
    """一个异常说给这一屏听。

    它自己两种语言都写了（`SaidToAPerson`）就照它说；不是的（第三方库的、只有一句技术
    原话的 —— Git 输出、stderr、`ExcType: …`），说 `otherwise` 那句人话，原话记进日志。
    原话是查问题的证据，不拼进界面上那句（一个 toast 里冒出半段 traceback，读的人只会更糊涂）。
    用在一个 `except` 同时接得住两种的地方 —— 比如 `except ValueError`，或者一个大部分
    子类是英文技术原话、个别子类是说给人听的基类。
    """
    # 框架那边（`core.materials.MaterialError` 之类）不 import 平台，但长着同一个形状 ——
    # 两种语言都写了的，就照它自己的说。
    said_in = getattr(error, "said_in", None)
    if isinstance(error, SaidToAPerson) or callable(said_in):
        return error.said_in(lang)  # type: ignore[attr-defined]
    _log.warning("%s (%s: %s)", say(otherwise, "en"), type(error).__name__, error)
    return say(otherwise, lang)
