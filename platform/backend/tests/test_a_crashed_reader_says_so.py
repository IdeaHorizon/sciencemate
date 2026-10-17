"""控制通道的读者自己炸了，不许装成「worker 把连接关了」—— 先吵，再 EOF。

2026-10-17 PC 真机：worker 把 result 写进了 events.jsonl，后端却报 harness_worker_unreachable（控制连接在它回话
之前关了）。读者 task 里的异常没人 await，一个字不留。两件事一条消息，查错的人就被指向了错的方向。
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from app.services import harness_sessions as hs


class _Channel(hs._WorkerChannel):
    usable = True

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def send_line(self, line: str) -> None:
        return None

    async def read_line(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        await asyncio.sleep(3600)
        return b""


def _session(channel) -> hs._ProjectHarnessSession:
    return hs._ProjectHarnessSession(
        project_id="p", session_id="s", owner_user_id="u", backend_id="b", backend_fingerprint="f",
        platform_context_hash=None, process=None, stderr_task=None, provider_secrets=[], channel=channel,
    )


async def test_an_exception_in_the_sink_is_logged_before_pending_rpcs_see_eof(caplog):
    session = _session(_Channel([json.dumps({"type": "transcript", "request_id": "r1", "event": {}}).encode() + b"\n"]))

    async def explode(_event: dict) -> None:
        raise RuntimeError("ingest blew up")

    async def noop(_event: dict) -> None:
        return None

    with caplog.at_level(logging.ERROR, logger=hs.logger.name):
        async with session._awaiting("r1", noop, explode, conversation=True) as queue:
            got = await asyncio.wait_for(queue.get(), timeout=5)
    assert got is session._EOF
    crashed = [r for r in caplog.records if "reader for p/s crashed" in r.getMessage()]
    assert crashed and "ingest blew up" in (crashed[0].exc_text or "")


async def test_a_clean_eof_does_not_cry_wolf(caplog):
    session = _session(_Channel([b""]))

    async def noop(_event: dict) -> None:
        return None

    with caplog.at_level(logging.ERROR, logger=hs.logger.name):
        async with session._awaiting("r1", noop, noop, conversation=True) as queue:
            got = await asyncio.wait_for(queue.get(), timeout=5)
    assert got is session._EOF
    assert not [r for r in caplog.records if "crashed" in r.getMessage()]


async def test_an_unreadable_worker_record_means_no_live_worker_not_a_dead_turn(monkeypatch, caplog):
    """接回活 worker 时盘上那行 stat 被拒（lujy 10-15 的目录、PC 判据 1 复现）：按没有活 worker 处理、照常 spawn，
    不许把 PermissionError 一路冒到 turn（那就是整轮在起 worker 之前就死）。"""

    async def denied(project_id, session_id, *, owner_user_id):
        raise PermissionError(5, "拒绝访问。", r"C:\afs\project-worktrees\x\y\.research\runtime\runs\orchestrator__p__session__s")

    monkeypatch.setattr(hs, "reattach_session", denied)
    manager = hs.HarnessSessionManager()
    with caplog.at_level(logging.WARNING, logger=hs.logger.name):
        got = await manager._adopt_locked("p", "s", owner_user_id="u")
    assert got is None
    assert [r for r in caplog.records if "treating as no live worker" in r.getMessage()]
