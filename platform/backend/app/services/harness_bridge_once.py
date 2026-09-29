"""问 harness 桥**一个**问题、拿一个答案 —— 一次性桥调用的唯一实现。

## 为什么要有这一处

桥有两种用法：`harness_runtime` 那种**流式**（一路读事件、边读边回放给用户），
和这里这种**一次性**（写一行请求、读到那个结果事件就收工）。一次性那种被手抄了
两遍 —— `harness_kb._ask` 和 `session_naming._ask_harness_for_title` —— 四十行
几乎逐字相同的舞步：起 `platform_runtime`、写一行 JSON、扫 stdout 找结果事件、
超时就杀。

两份抄件里**同一个字段被同样地丢掉了**：`stdout, _ = await process.communicate(...)`
——stderr 明明用 `PIPE` 收了，然后扔掉。代价在 2026-09-10 兑现：Windows 上装好的
应用里 session autoname 失败，日志里只有一句

    app.services.session_naming.SessionNamingError: runtime_error

`runtime_error` 是桥的 error 事件里的 code，而**解释它的那几行 traceback 就在被
丢掉的 stderr 里**。一个把证据丢掉的失败，等于一个查不了的失败。

所以这里做两件事，一次做对：

1. **收口**：一次性调用只有这一份实现。两份抄件各自演化时不会有任何东西报错
   （`harness_runtime` 早就会 drain stderr 并把它带进错误，两份抄件却没跟上 ——
   这正是"一个问题几份抄件就有几个答案"的样子）。
2. **失败必须说出为什么**：stderr 一律 drain、按 `secret` 打码、进每一条错误消息。
   桥只给了个 code（没有 message）时，stderr 是唯一的线索。

## 不做什么

不决定超时多少、不决定哪个文件算"这是个 harness checkout"、不决定抛什么异常 ——
那些答案属于调用方，各有各的合理值（命名 150s、KB 查询另一个数）。这里只负责
"怎么问、怎么把失败原样带回来"。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.services.redaction import PROTOCOL_LINE_LIMIT_BYTES

#: 错误消息里最多带多少 stderr。够看清一段 traceback，又不至于把日志冲爆。
STDERR_TAIL_CHARS = 1200


async def ask_once(
    request: dict[str, Any],
    *,
    expect: str,
    error: Callable[[str], Exception],
    root: Path,
    python: str,
    child_env: dict[str, str],
    timeout_s: float,
    secret: str | None = None,
) -> dict[str, Any]:
    """写一行请求，等 `expect` 那个事件。任何一种失败都带上 stderr。

    `error` 是调用方的异常工厂 —— 调用方的错误类型是它自己契约的一部分，不该被
    收口这件事改掉。`secret` 给了就从 stderr 和消息里打码（子进程的环境里带着
    模型凭据，traceback 里出现过）。
    """
    def say(headline: str, stderr: str) -> Exception:
        tail = _redact(stderr, secret).strip()
        if tail:
            detail = (f"{headline}\n  桥的 stderr（末 {STDERR_TAIL_CHARS} 字）："
                      f"\n{tail[-STDERR_TAIL_CHARS:]}")
        else:
            detail = f"{headline}（桥的 stderr 一个字都没有）"
        return error(_redact(detail, secret))

    process = await asyncio.create_subprocess_exec(
        python, "-m", "platform_runtime",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(root),
        env=child_env,
        limit=PROTOCOL_LINE_LIMIT_BYTES,
    )
    line = (json.dumps(request, ensure_ascii=False) + "\n").encode()
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(line), timeout=timeout_s)
    except TimeoutError as exc:
        process.kill()
        # 杀完还要收尸并把它已经说过的话拿到手 —— 超时最需要 stderr。
        _, late = await process.communicate()
        raise say(f"harness 桥 {timeout_s:.0f} 秒内没回答（op={request.get('op')!r}）",
                  (late or b"").decode("utf-8", errors="replace")) from exc

    stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
    for raw in (stdout_bytes or b"").decode("utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == expect:
            return event
        if event.get("type") == "error":
            # 桥的错误原样带上来。吞掉它就等于把"查询坏了"变成"知识库是空的"，
            # 而这两件事看起来一模一样。code 常常只有一个词（真机上见过光秃秃的
            # `runtime_error`）—— 所以 stderr 必须跟着一起走。
            told = str(event.get("message") or event.get("code") or "unknown")
            refused = say(told, stderr)
            # 桥说的 code 挂在异常上：调用方按「哪一种拒绝」回 HTTP 状态（没有这一条 / 已经裁过 /
            # 缺理由），不必去解析给人看的那句话。
            refused.code = str(event.get("code") or "")  # type: ignore[attr-defined]
            raise refused
    raise say(f"harness 桥没给出 {expect}（退出码 {process.returncode}）", stderr)


def _redact(text: str, secret: str | None) -> str:
    if not secret or not text:
        return text
    return text.replace(secret, "[REDACTED]")
