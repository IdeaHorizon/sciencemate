"""Fail-closed transcript redaction performed before event persistence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

REDACTED = "[REDACTED]"
REDACTED_CONNECTION = "[REDACTED_CONNECTION_STRING]"
MAX_DEPTH = 12
MAX_COLLECTION_ITEMS = 200
MAX_STRING_LENGTH = 4_000
#: 有意携带的长文本（diff 正文）。4000 字符是给**任意**字符串兜底的上限，
#: 套在这类字段上等于把证据剪掉一大半 —— 而且是静默剪：调用方照样报
#: "未截断"，读者会把断口当成"改动到此为止"。它们的体积由产出方各自限额，
#: 这里只留一个防爆的天花板（与 ProjectRepository.diff 的 200KB 对齐）。
MAX_LONG_TEXT_LENGTH = 200_000
LONG_TEXT_KEYS = {"patch"}

#: 协议单行读上限 —— 与"最大合法事件"同源推导，别在两边各写一个数。
#:
#: 2026-08-19 事故：发送端按上面的 200K 字符放行 diff 正文（设计上合法的
#: 流量），接收端 socket 却吃 asyncio 的 64KB 默认 limit —— stdio 那条管子
#: 当年设了 4MB，P0-2 换 socket 时这个数字没跟过来。一条 76 分钟的研究 run
#: 被一行合法事件杀死。同一个契约两份抄件，分叉时两边都不报错。
#:
#: 4MB = 200K 字符 × 4 字节（UTF-8 最坏）× 若干长文本字段 + 信封余量。
#: import 时校验这层推导关系：谁改了脱敏上限而没顾及传输，当场炸。
PROTOCOL_LINE_LIMIT_BYTES = 4 * 1024 * 1024

assert PROTOCOL_LINE_LIMIT_BYTES >= MAX_LONG_TEXT_LENGTH * 4 + 65_536, (
    "传输单行上限必须容得下最大合法脱敏事件（MAX_LONG_TEXT_LENGTH×4 + 信封）"
)

_SENSITIVE_KEYS = {
    "accesskey",
    "accesstoken",
    "apikey",
    "apitoken",
    "authorization",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "connectionstring",
    "cookie",
    "credential",
    "databaseurl",
    "dsn",
    "idtoken",
    "password",
    "passwd",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
}
_SENSITIVE_KEY_SUFFIXES = (
    "accesskey",
    "accesstoken",
    "apikey",
    "apitoken",
    "authorization",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "connectionstring",
    "cookie",
    "credential",
    "databaseurl",
    "dsn",
    "idtoken",
    "password",
    "passwd",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_API_TOKEN = re.compile(r"\b(?:sk|pk)_[A-Za-z0-9_-]{8,}\b|\bsk-[A-Za-z0-9_-]{8,}\b")
_CONNECTION = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp)://[^\s]+"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL
)
#: AWS access key id。补丁正文从前在仓库层另扫一遍这类模式再把整个文件扣下
#: （`_secret_like`）；"人能看见什么"只在这一层判，仓库层只管写边界。
_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
#: GitHub 的个人 / OAuth / 应用 token 与细粒度 PAT（``ghp_…``、``github_pat_…``）。
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


#: 「长得像密钥的值」按这个次序替换。界面入库（``RedactionPolicy``）与诊断包
#: （``session_diagnostics``）共用这一张表 —— 两边各写一份，新加的形状就只有一边认得。
_SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_PRIVATE_KEY, REDACTED),
    (_AWS_ACCESS_KEY, REDACTED),
    (_GITHUB_TOKEN, REDACTED),
    (_CONNECTION, REDACTED_CONNECTION),
    (_BEARER, REDACTED),
    (_API_TOKEN, REDACTED),
)
#: JSON 里的一个字符串字段 ``"名字": "值"``（名字与值都允许转义）。
_JSON_STRING_FIELD = re.compile(r'"((?:[^"\\]|\\.)*)"(\s*:\s*)"(?:[^"\\]|\\.)*"')


def is_sensitive_key(key: object) -> bool:
    """这个字段名是不是用来装密钥的（password / token / cookie / …_api_key …）。"""
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def scrub_secret_values(text: str) -> tuple[str, int]:
    """把长得像密钥的值（私钥、AWS key、连接串、Bearer、sk-/pk_ token）替换掉，返回替换次数。"""
    count = 0
    for pattern, replacement in _SECRET_VALUE_PATTERNS:
        text, hits = pattern.subn(replacement, text)
        count += hits
    return text, count


def scrub_secrets_in_text(text: str) -> tuple[str, int]:
    """一整段文本（日志、JSON 行、整份 JSON）里的密钥：名字像密钥的 JSON 字段 + 长得像密钥的值。

    只管「密钥不出门」，不截断、不缩路径 —— 那是界面展示的事，诊断包要的是原样的证据。
    """
    count = 0

    def field(match: re.Match[str]) -> str:
        nonlocal count
        if not is_sensitive_key(match.group(1)):
            return match.group(0)
        count += 1
        return f'"{match.group(1)}"{match.group(2)}"{REDACTED}"'

    text = _JSON_STRING_FIELD.sub(field, text)
    text, hits = scrub_secret_values(text)
    return text, count + hits


@dataclass(frozen=True, slots=True)
class RedactionResult:
    value: Any
    warnings: tuple[str, ...]


class RedactionPolicy:
    """Sanitize nested JSON-like values and never pass unsupported values through."""

    def sanitize(
        self, value: Any, *, max_string_length: int = MAX_STRING_LENGTH
    ) -> RedactionResult:
        """脱敏。`max_string_length` 是**顶层**字符串的上限 —— 直接送一份 diff
        进来的调用方（不带 key，所以吃不到 `LONG_TEXT_KEYS`）用它抬限额。"""
        warnings: list[str] = []
        try:
            sanitized = self._sanitize(
                value, path="$", depth=0, warnings=warnings, limit=max_string_length
            )
        except Exception:
            sanitized = {"redacted": True}
            warnings.append("redaction_failed_closed")
        return RedactionResult(sanitized, tuple(dict.fromkeys(warnings)))

    def _sanitize(
        self,
        value: Any,
        *,
        path: str,
        depth: int,
        warnings: list[str],
        limit: int = MAX_STRING_LENGTH,
    ) -> Any:
        if depth > MAX_DEPTH:
            warnings.append("maximum_depth")
            return REDACTED
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self._sanitize_string(value, path=path, warnings=warnings, limit=limit)
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for index, (key, child) in enumerate(value.items()):
                if index >= MAX_COLLECTION_ITEMS:
                    warnings.append("object_truncated")
                    break
                safe_key = str(key)[:200]
                child_path = f"{path}.{safe_key}"
                normalized_key = re.sub(r"[^a-z0-9]", "", safe_key.lower())
                if is_sensitive_key(safe_key):
                    output[safe_key] = REDACTED
                    warnings.append("sensitive_field")
                else:
                    output[safe_key] = self._sanitize(
                        child,
                        path=child_path,
                        depth=depth + 1,
                        warnings=warnings,
                        limit=(
                            MAX_LONG_TEXT_LENGTH
                            if normalized_key in LONG_TEXT_KEYS
                            else limit
                        ),
                    )
            return output
        if isinstance(value, (list, tuple)):
            if len(value) > MAX_COLLECTION_ITEMS:
                warnings.append("array_truncated")
            return [
                self._sanitize(
                    child,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    warnings=warnings,
                    limit=limit,
                )
                for index, child in enumerate(value[:MAX_COLLECTION_ITEMS])
            ]
        warnings.append("unsupported_type")
        return REDACTED

    def _sanitize_string(
        self, value: str, *, path: str, warnings: list[str], limit: int = MAX_STRING_LENGTH
    ) -> str:
        sanitized, hits = scrub_secret_values(value)
        if hits:
            warnings.append("sensitive_value")

        if sanitized.startswith("/") or _WINDOWS_PATH.match(sanitized):
            normalized = sanitized.replace("\\", "/")
            parts = [part for part in normalized.split("/") if part]
            sanitized = "…/" + "/".join(parts[-2:]) if parts else "…"
            warnings.append("path_minimized")

        if len(sanitized) > limit:
            sanitized = sanitized[:limit] + "…[TRUNCATED]"
            warnings.append("string_truncated")
        return sanitized


DEFAULT_REDACTION_POLICY = RedactionPolicy()
