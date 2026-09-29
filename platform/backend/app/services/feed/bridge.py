"""资讯流经桥调模型的**唯一**一处实现。

## 为什么抽出来

`digest.py` 里原本自带一份 spawn 子进程 + 传凭据 + 读事件的代码，它逐字来自
`session_naming.py`。现在挖掘要再调一次模型 —— 抄第三份的话，超时怎么定、
凭据怎么擦、失败怎么归因这些判断就有了三个会各自演化的答案，而分叉时没有
任何一层会报错（各自都能跑）。

## 这一层的边界

只做三件事：把凭据交给桥、等一个结果、把结果或失败翻译回来。**不判断该不该
调**（那是调用方的事），**不重试**（资讯流的模型调用都是尽力而为，失败下次
再说；留着重试只会把一次慢调用乘上几倍然后照样撞外层超时）。

所有经由这里的 op 都是**有界的单次调用**：零工具、不建 run 目录、不进 KB、
不留执行事件。要工具、要留痕的，走的是研究那条路，不是这里。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import data_root, settings
from app.models.model_backend import ModelBackendConfig
from app.models.user import User
from app.services.harness_runtime import (
    _provider_base_url,
    harness_subprocess_env,
    the_interpreter_that_runs_the_harness,
)
from app.services.model_backends import resolved_api_key, select_effective_backend

logger = logging.getLogger(__name__)

#: 资讯流自动挖掘用哪个模型角色。取值来自 harness 的角色目录
#: （`shared/model_roles.yaml`）—— 平台不枚举角色。
FEED_CURATION_ROLE = "feed_curation"
REASONING_ROLE = "reasoning"
LITERATURE_ENV_KEYS = frozenset({
    "SEMANTIC_SCHOLAR_API_KEY", "SEMANTIC_SCHOLAR_MIN_INTERVAL",
    "HARNESS_ARXIV_TIMEOUT_SECONDS", "HARNESS_ARXIV_MIN_INTERVAL_SECONDS",
    "CNKI_COOKIE_PATH",
    "OPENALEX_API_KEY",
})

#: 内层（模型调用）必须严格小于外层（子进程），否则永远是子进程先被杀掉，
#: 拿到的是一句 "timed out" 而不是 harness 那条说得清是连不上、被拒、还是
#: 真的慢的错误。
MODEL_TIMEOUT_SECONDS = 120
SUBPROCESS_TIMEOUT_SECONDS = 150
assert MODEL_TIMEOUT_SECONDS < SUBPROCESS_TIMEOUT_SECONDS


class BridgeUnavailable(RuntimeError):
    """这次调用做不了。**不是错误页**：调用方照常工作，只是这次没有加工层。

    消息里要说清是谁的问题（没配模型 / 桥关了 / 超时），因为这三种的处置
    完全不同，而从外面看它们长得一样。
    """


async def curation_backend(db: AsyncSession, user: User) -> ModelBackendConfig | None:
    """返回项目画像使用的模型；专用资讯模型优先，主推理模型兜底。

    用户开启项目画像检索后，已经明确授权平台读取其Project并生成检索词。
    因此没有单独配置 ``feed_curation`` 时可以复用其 reasoning 连接；若配置了
    专用模型，仍始终优先使用专用连接。
    """
    backend = await select_effective_backend(db, user, role=FEED_CURATION_ROLE)
    if backend is not None:
        return backend
    return await select_effective_backend(db, user, role=REASONING_ROLE)


async def call(
    *,
    op: str,
    payload: dict[str, Any],
    result_type: str,
    backend: ModelBackendConfig,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """跑一次 `op`，返回它的结果事件。"""
    if not settings.harness_bridge_enabled:
        raise BridgeUnavailable("harness bridge is disabled")
    api_key = resolved_api_key(backend)
    base_url = _provider_base_url(backend)
    if not api_key or not base_url:
        raise BridgeUnavailable("model backend has no usable credentials")

    root = Path(settings.harness_root).expanduser().resolve()
    if not (root / "core" / "llm.py").is_file():
        raise BridgeUnavailable("HARNESS_ROOT is not a valid harness checkout")
    python = the_interpreter_that_runs_the_harness()

    request = {**payload, "op": op, "request_id": f"{op}-{uuid4().hex}"}
    child_env = harness_subprocess_env(root, passthrough=LITERATURE_ENV_KEYS, extra={
        "LLM_API_KEY": api_key,
        "LLM_BASE_URL": base_url,
        "LLM_MODEL": backend.model,
        "LLM_TIMEOUT": str(MODEL_TIMEOUT_SECONDS),
        "LLM_MAX_RETRIES": "0",
        "HARNESS_STATE_ROOT": str(data_root("state").resolve()),
        # 让学术搜索复用 literature 原有 strategy_generator；它沿用同一
        # 个 reasoning 后端，但使用 literature 历史的 STRATEGY_LLM 契约。
        "STRATEGY_LLM_BASE": base_url,
        "STRATEGY_LLM_KEY": api_key,
        "STRATEGY_LLM_MODEL": backend.model,
    })

    process = await asyncio.create_subprocess_exec(
        python, "-m", "platform_runtime",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(root),
        env=child_env,
        limit=4 * 1024 * 1024,
    )
    stdout_lines: list[str] = []
    # ── stderr 必须一直有人在排（2026-08-25）────────────────────────────────
    #
    # 这里原本是 `process.communicate()`，它同时排空 stdout 和 stderr。改成手写
    # 的 readline 循环（为了拿流式进度）就只剩 stdout 有人读了，而 stderr 仍然
    # 是 PIPE。实测过阈值：stderr 写到 9MB（asyncio StreamReader 的高水位是
    # `limit` 的两倍 = 8MB）时读循环会一路超时，**而且随后的
    # `kill(); await wait()` 也再不返回** —— 那个请求就永久挂在那儿，不是慢，
    # 是不收场。
    #
    # 今天多半打不着：`platform_runtime.main()` 整段跑在 `_silence_process_fds()`
    # 里，fd 2 指着 /dev/null，子进程实际几乎不往 stderr 写。但"今天恰好没人写"
    # 不是一条性质 —— 把 communicate() 那条性质原样接回来：一个并发任务读到 EOF。
    stderr_drain = (
        asyncio.create_task(process.stderr.read()) if process.stderr is not None else None
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
        await process.stdin.drain()
        process.stdin.close()
        deadline = asyncio.get_running_loop().time() + SUBPROCESS_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            if not line:
                break
            decoded = line.decode(errors="replace")
            stdout_lines.append(decoded)
            if on_progress is not None:
                try:
                    event = json.loads(decoded)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "literature_search_progress":
                    await on_progress(event)
        remaining = deadline - asyncio.get_running_loop().time()
        await asyncio.wait_for(process.wait(), timeout=max(0.1, remaining))
    except (TimeoutError, asyncio.TimeoutError) as exc:
        process.kill()
        await process.wait()
        raise BridgeUnavailable(f"{op} timed out") from exc
    finally:
        if stderr_drain is not None and not stderr_drain.done():
            stderr_drain.cancel()

    return _read_result("".join(stdout_lines), result_type=result_type, op=op)


def _read_result(stdout: str, *, result_type: str, op: str) -> dict[str, Any]:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == result_type:
            return event
        if event.get("type") == "error":
            # harness 说得清是什么错，原样带上 —— 翻译成一句笼统的失败，
            # 就把"谁的锅"这个信息在这一层丢掉了。
            raise BridgeUnavailable(
                str(event.get("message") or event.get("code") or "unknown")
            )
    raise BridgeUnavailable(f"harness bridge returned no {result_type} for {op}")
