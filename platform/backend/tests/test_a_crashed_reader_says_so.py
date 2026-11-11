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


async def test_an_exception_in_the_sink_is_logged_without_destroying_control(caplog):
    session = _session(_Channel([json.dumps({"type": "transcript", "request_id": "r1", "event": {}}).encode() + b"\n"]))

    async def explode(_event: dict) -> None:
        raise RuntimeError("ingest blew up")

    async def noop(_event: dict) -> None:
        return None

    with caplog.at_level(logging.ERROR, logger=hs.logger.name):
        async with session._awaiting("r1", noop, explode, conversation=True) as queue:
            got = await asyncio.wait_for(queue.get(), timeout=5)
            assert got['type'] == 'transcript'
            assert not session._reader.done()
    session._reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session._reader
    errors = [r for r in caplog.records if "control reader remains available" in r.getMessage()]
    assert errors and "ingest blew up" in (errors[0].exc_text or "")


async def test_a_clean_eof_does_not_cry_wolf(caplog):
    session = _session(_Channel([b""]))

    async def noop(_event: dict) -> None:
        return None

    with caplog.at_level(logging.ERROR, logger=hs.logger.name):
        async with session._awaiting("r1", noop, noop, conversation=True) as queue:
            got = await asyncio.wait_for(queue.get(), timeout=5)
    assert got is session._EOF
    assert not [r for r in caplog.records if "crashed" in r.getMessage()]


async def test_invalid_wrapper_preserves_management_rpc_and_never_claims_completion(tmp_path, monkeypatch):
    from app.services import harness_transcript_ingest as ingest

    class Channel(_Channel):
        def __init__(self):
            self.queue = asyncio.Queue()
        async def read_line(self):
            return await self.queue.get()
        def emit(self, value):
            self.queue.put_nowait(json.dumps(value).encode() + b'\n')

    path = tmp_path / 'transcript.jsonl'
    path.write_text('{}\n')
    monkeypatch.setattr(ingest, '_approved_runtime_root', lambda *args: tmp_path)
    channel = Channel()
    session = _session(channel)
    async def sink(event):
        if event.get('type') == 'transcript':
            ingest.resolve_wrapper(event, project_id='p', session_id='s')
    async def noop(event):
        return None

    try:
        async with session._awaiting('turn', noop, sink, conversation=True) as turn:
            async with session._awaiting('stop', noop, noop, conversation=False) as control:
                channel.emit({'type': 'transcript', 'request_id': 'turn', 'event': {},
                    'transcript_path': str(path), 'byte_start': 10, 'byte_end': 5})
                channel.emit({'type': 'result', 'request_id': 'stop', 'data': {'delivered': True}})
                result = await session._await_terminal(control, terminal_types={'result'}, timeout_seconds=2)
                assert result['data']['delivered'] is True
                assert not session._reader.done()
            channel.emit({'type': 'result', 'request_id': 'turn', 'data': {'status': 'completed'}})
            with pytest.raises(hs.HarnessSessionError) as caught:
                await session._await_terminal(turn, terminal_types={'result'}, timeout_seconds=2)
            assert caught.value.code == 'harness_transcript_ingest_failed'
            assert caught.value.details['byte_start'] == 10
            assert caught.value.details['byte_end'] == 5
            assert caught.value.details['safe_to_resubmit'] is False
            assert caught.value.details['control_connected'] is True
            assert 'invalid byte range' in caught.value.cause
    finally:
        session._reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session._reader
    assert not session._ingest_errors


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
