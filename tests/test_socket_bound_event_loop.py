"""Keep Winsock connection setup socket-local without losing async processes."""

from __future__ import annotations

import asyncio
import socket
import sys

import pytest

from shared.lib import event_loop


def test_each_stream_socket_is_prepared_before_its_connect(monkeypatch):
    events = []

    class NativeProactor:
        def connect(self, conn, address):
            events.append(("connect", conn, address))
            return "native-future"

    class Proactor(event_loop._SocketBoundConnectEx, NativeProactor):
        pass

    monkeypatch.setattr(
        event_loop, "_prepare_connectex", lambda conn: events.append(("lookup", conn))
    )
    with (
        socket.socket() as first,
        socket.socket() as second,
        socket.socket(type=socket.SOCK_DGRAM) as udp,
    ):
        address = ("127.0.0.1", 9000)
        for conn in (first, second, udp):
            assert Proactor().connect(conn, address) == "native-future"
        assert events == [
            ("lookup", first),
            ("connect", first, address),
            ("lookup", second),
            ("connect", second, address),
            ("connect", udp, address),
        ]


def test_provider_lookup_failure_does_not_start_connect(monkeypatch):
    class NativeProactor:
        def connect(self, *_args):
            pytest.fail("a rejected provider lookup must not dispatch ConnectEx")

    class Proactor(event_loop._SocketBoundConnectEx, NativeProactor):
        pass

    def fail(_conn):
        raise OSError(10057, "provider rejected the socket")

    monkeypatch.setattr(event_loop, "_prepare_connectex", fail)
    with socket.socket() as conn, pytest.raises(OSError, match="provider rejected"):
        Proactor().connect(conn, ("127.0.0.1", 9000))


def test_process_loop_connects_new_sockets_and_runs_async_subprocesses():
    async def exchange(reader, writer):
        assert await reader.readexactly(4) == b"ping"
        writer.write(b"pong")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def scenario():
        loop = asyncio.get_running_loop()
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.ProactorEventLoop)
        server = await asyncio.start_server(exchange, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            for _ in range(8):
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), 5
                )
                writer.write(b"ping")
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(4), 5) == b"pong"
                writer.close()
                await writer.wait_closed()
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "print('subprocess-ok')",
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), 10)
            assert proc.returncode == 0
            assert stdout.strip() == b"subprocess-ok"
        finally:
            server.close()
            await server.wait_closed()

    event_loop.run(scenario())
