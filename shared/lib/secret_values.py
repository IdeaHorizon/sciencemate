"""按「原值」脱敏的那一条规则：哪些配置值算得上密钥、怎么从文本里抹掉。worker 与后端共用这一份。

#1228（2026-10-17 PC 真机）：假模型的 api_key 配成了 ``"k"``。四处按原值脱敏的代码各自做子串替换，
事件流里**每一个** ``k`` 都成了 ``[REDACTED]``：私有 state 目录名 ``hf-state-6k8tx30o`` 被改成
``hf-state-6[REDACTED]8tx30o``，后端据此判「转录逃出 state 根」，读者炸，用户看到的是
``harness_worker_unreachable`` —— 症状指向一件完全无关的事。在设置里填错 / 填短一个 key
（一个字母、``sk-``、空格）就会撞上。

规则：去掉首尾空白后短于 ``MIN_SECRET_CHARS`` 的值**不按原值抹**。真 key 没有这么短；短到这个程度，
按原值抹掉的只会是正文。它也不会因此漏出去：长成 key 样子的（``sk-…``、Bearer）另有按形状的规则管。

长的先替：一个值是另一个的前缀时，先替短的会留下一截尾巴。
"""
from __future__ import annotations

from collections.abc import Iterable

#: 8：现有各家 key 远长于此；诊断包从 09-23 起就用这个门槛（`session_diagnostics.Redactor`）。
MIN_SECRET_CHARS = 8
REDACTED = "[REDACTED]"


def split_secret_values(values: Iterable[object]) -> tuple[list[str], int]:
    """（值得按原值抹的那些，长的在前；太短被放过的个数）。非字符串与空值不计。"""
    kept: set[str] = set()
    too_short = 0
    for value in values:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if not stripped:
            continue
        if len(stripped) < MIN_SECRET_CHARS:
            too_short += 1
            continue
        kept.add(stripped)
    return sorted(kept, key=len, reverse=True), too_short


def secret_values(values: Iterable[object]) -> list[str]:
    return split_secret_values(values)[0]


def redact_values(text: str, secrets: Iterable[str]) -> str:
    """把 `secrets`（已经过 `secret_values` 筛过、长的在前）逐个从 `text` 里抹掉。"""
    return redact_values_counted(text, secrets)[0]


def redact_values_counted(data, secrets: Iterable[str]):
    """同上，另外数抹了几处。`data` 可以是 str 或 bytes（诊断包按字节抹，二进制原样往返）。"""
    as_bytes = isinstance(data, bytes)
    redacted = REDACTED.encode() if as_bytes else REDACTED
    count = 0
    for secret in secrets:
        if not secret:
            continue
        needle = secret.encode() if as_bytes else secret
        hits = data.count(needle)
        if hits:
            data = data.replace(needle, redacted)
            count += hits
    return data, count
