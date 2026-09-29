"""Process-local credentials that must not be inherited by tool subprocesses.

The App Server runtime starts a dedicated Worker subprocess per harness turn.
It may inject provider credentials into that process environment at spawn time,
but model-controlled tools must not be able to recover those credentials through
``env`` or a child Python process.  The platform bridge therefore moves secrets
from ``os.environ`` into this module before tool registration/execution.

This is intentionally process-local, small, and not a general secret manager.
Production credentials should still be short-lived Gateway tokens.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

_SECRETS: dict[str, str] = {}


def get(name: str, default: str = "") -> str:
    return _SECRETS.get(name, default)


def is_set(name: str) -> bool:
    return bool(get(name))


def install(name: str, value: str) -> None:
    """装一条**活到进程结束**的凭据。

    与 `scoped()` 的区别是所有权：scoped 归还，install 不归还。模型角色的
    凭据属于后者 —— 它们随交付通道一次性进来、通道变量当场被弹出，没有
    "还回去"这回事。

    ⚠️ 别拿 `scoped(...).__enter__()` 冒充它：不持有那个 context manager 的
    引用，它会被 GC，`finally` 当场把刚装上的密钥擦掉，而且不报任何错
    （2026-08-22 冒烟测试抓到的就是这个 —— 解析全对、绑定全在、key 是空串）。
    """
    if value:
        _SECRETS[name] = value
    else:
        _SECRETS.pop(name, None)


@contextmanager
def scoped(name: str, value: str) -> Iterator[None]:
    """Install one secret for the current Worker lifetime, then erase it."""
    had_previous = name in _SECRETS
    previous = _SECRETS.get(name, "")
    if value:
        _SECRETS[name] = value
    else:
        _SECRETS.pop(name, None)
    try:
        yield
    finally:
        if had_previous:
            _SECRETS[name] = previous
        else:
            _SECRETS.pop(name, None)
