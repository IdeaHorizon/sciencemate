"""模型命令在哪里跑 —— 执行目标（``docs/RFC_WINDOWS_EXECUTION_TARGETS_20261030.md`` D1/D2）。

三种目标：

* ``local``：本机，后端由 ``HARNESS_EXECUTOR`` 选（darwin / linux / win32）。默认。
* ``wsl:<发行版>``：Windows 主机上的一个 WSL 发行版。worker（Medium IL）经 ``wsl.exe`` 进去，
  发行版里套 linux 后端（``core.isolation.wsl``）。
* ``ssh:<机器>``：词表先占位，后端未实现（RFC P4）。选了它，命令以 ``spawn_failed`` 明说，
  **不悄悄回落到本机**。

## 谁来定

目标由人定，不由模型定（D2）。平台把项目配置里的 ``execution_target`` 随每条请求声明给
worker（``platform_runtime`` 收到后调 :func:`declare`），与自主档同一条通道 —— 改设置不用
重启 worker。CLI / 测试没有平台时读环境变量 ``HARNESS_EXECUTION_TARGET``。都没有 = ``local``。

声明是**进程级**的：一个 worker 只服务一个会话，会话里的所有节点 run 在同一个进程里，
命令都经同一个咽喉（``shared.lib.cancellable_subprocess.spawn_and_wait``）落到
``core.isolation.select_backend``，后者在这里问一次「现在是哪个目标」。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

ENV = "HARNESS_EXECUTION_TARGET"

KINDS = ("local", "wsl", "ssh")

#: WSL 发行版名与机器名：字母数字开头，后面字母数字 . _ -。``wsl --install -d`` 与
#: ``wsl --import`` 接受的名字都在这个集合里；空格、斜杠、冒号一律不收（它们会进命令行）。
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class ExecutionTargetError(ValueError):
    """目标写法不合法。报错里给出合法写法。"""


@dataclass(frozen=True)
class ExecutionTarget:
    kind: str
    name: str = ""

    @property
    def spec(self) -> str:
        """规范写法：``local`` / ``wsl:Ubuntu-24.04`` / ``ssh:hpc``。"""
        return self.kind if self.kind == "local" else f"{self.kind}:{self.name}"

    @property
    def is_local(self) -> bool:
        return self.kind == "local"


LOCAL = ExecutionTarget("local")


def parse(raw: object) -> ExecutionTarget:
    """``None`` / 空串 / ``"local"`` → 本机；``"wsl:<发行版>"`` / ``"ssh:<机器>"``；其余报错。"""
    if raw is None:
        return LOCAL
    if not isinstance(raw, str):
        raise ExecutionTargetError(f"execution target must be a string, got {type(raw).__name__}")
    text = raw.strip()
    if not text or text == "local":
        return LOCAL
    kind, sep, name = text.partition(":")
    if not sep or kind not in KINDS or kind == "local" or not _NAME.fullmatch(name):
        raise ExecutionTargetError(
            f"{raw!r} is not an execution target; valid forms: 'local', 'wsl:<distro>', "
            "'ssh:<machine>' (names: letters, digits, '.', '_', '-')"
        )
    return ExecutionTarget(kind, name)


_declared: ExecutionTarget | None = None


def declare(raw: object) -> ExecutionTarget:
    """平台声明这个会话**此刻**的目标。``None`` = 这条请求没谈目标，保持原样。

    返回生效后的目标。写法不合法时抛 :class:`ExecutionTargetError`，原声明不动。
    """
    global _declared
    if raw is None:
        return current()
    _declared = parse(raw)
    return _declared


def current() -> ExecutionTarget:
    """平台声明 > ``HARNESS_EXECUTION_TARGET`` > 本机。环境变量写错时报错，不当成本机。"""
    if _declared is not None:
        return _declared
    return parse(os.environ.get(ENV))


def _reset_for_tests() -> None:
    global _declared
    _declared = None


__all__ = [
    "ENV",
    "ExecutionTarget",
    "ExecutionTargetError",
    "KINDS",
    "LOCAL",
    "current",
    "declare",
    "parse",
]
