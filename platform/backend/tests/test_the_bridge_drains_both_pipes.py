"""子进程的 stderr 必须一直有人在排 —— 否则请求不是变慢，是不收场。

## 根因（2026-08-25 实测）

`bridge.call` 原本用 `process.communicate()`，它同时排空 stdout 和 stderr。
学术搜索要流式进度，把它改写成了手写的 `readline` 循环 —— 只读 stdout，而
`stderr=PIPE` 还在。

asyncio 的 StreamReader 会先把 stderr 缓进内存，所以小量输出看不出问题（实测
100KB / 1MB 都正常）。越过高水位（`limit` 的两倍 = 8MB）之后 transport 暂停读
管道，子进程堵在写 stderr 上：

    读循环等不到 stdout   → SUBPROCESS_TIMEOUT_SECONDS 到点
    `kill(); await wait()` → **也不返回**（暂停的 transport 永远收不了场）

也就是说那个请求永久挂住，连超时那条错误路径都走不完。

## 为什么这条测试值得存在

今天这条路多半打不着：`platform_runtime.main()` 整段跑在 `_silence_process_fds()`
里，fd 2 指着 /dev/null。但"今天恰好没人往 stderr 写"是一个巧合，不是一条性质
—— 哪天有个原生库在那层生效之前先吐一片，或者有人把静默范围改小，症状就是一条
永远不返回的请求，且没有任何报错。判据钉在**行为**上（真起一个吐 9MB stderr 的
子进程），不是钉在"源码里有没有 drain 这个词"。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

#: 高于 asyncio StreamReader 的高水位（bridge 里 `limit=4MB`，暂停阈值是它的两倍）。
STDERR_BYTES = 9 * 1024 * 1024


def _fake_harness(root: Path) -> None:
    """一个最小的 harness checkout：够过 bridge 的入口校验，行为由我们定。"""
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "core" / "llm.py").write_text("", encoding="utf-8")
    (root / "platform_runtime.py").write_text(
        "import json, sys\n"
        "sys.stdin.readline()\n"
        f"sys.stderr.write('x' * {STDERR_BYTES})\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write(json.dumps({'type': 'probe_result', 'ok': True}) + '\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_a_chatty_child_still_gets_its_result_read(tmp_path, monkeypatch) -> None:
    from app.config import settings
    from app.services.feed import bridge

    _fake_harness(tmp_path)
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_root", str(tmp_path))
    monkeypatch.setattr(settings, "harness_python", sys.executable)
    # 凭据这一层不是本条的被测面，直接给到位。
    monkeypatch.setattr(bridge, "resolved_api_key", lambda backend: "test-key")
    monkeypatch.setattr(bridge, "_provider_base_url", lambda backend: "http://localhost:1/v1")

    class _Backend:
        model = "test-model"

    # 外层再套一道 wait_for：这条缺陷复现时**连超时路径都走不完**，不套的话
    # 变异版本会把整个 job 挂死（那是"慢"的样子，不是"红"的样子）。
    result = await asyncio.wait_for(
        bridge.call(op="probe", payload={}, result_type="probe_result", backend=_Backend()),
        timeout=60,
    )
    assert result == {"type": "probe_result", "ok": True}, (
        "子进程往 stderr 写了 9MB 之后，stdout 上那行结果读不回来了 —— "
        "stderr 这条管道没人排，子进程堵在写它上面。"
    )
