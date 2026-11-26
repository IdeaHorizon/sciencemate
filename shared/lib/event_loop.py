"""Process entry-point loops, including socket-bound Windows ConnectEx lookup.

Some Winsock providers initialize connection state during the extension lookup.
CPython caches the entry point from a donor socket; querying each actual socket
before dispatch preserves that provider contract while retaining IOCP and async
subprocess support. No event-loop policy or third-party class is monkeypatched.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")


def _prepare_connectex(conn: socket.socket) -> None:
    import ctypes
    import uuid
    from ctypes import wintypes

    winsock = ctypes.WinDLL("ws2_32", use_last_error=True)
    ioctl = winsock.WSAIoctl
    ioctl.argtypes = (
        ctypes.c_size_t,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    ioctl.restype = ctypes.c_int
    winsock.WSAGetLastError.restype = ctypes.c_int
    guid = ctypes.create_string_buffer(
        uuid.UUID("25a207b9-ddf3-4660-8ee9-76e58c74063e").bytes_le,
        16,
    )
    entry = ctypes.c_void_p()
    returned = wintypes.DWORD()
    # SIO_GET_EXTENSION_FUNCTION_POINTER, WSAID_CONNECTEX. The lookup belongs
    # to this socket even when the returned address equals the cached address.
    if (
        ioctl(
            conn.fileno(),
            0xC8000006,
            guid,
            16,
            ctypes.byref(entry),
            ctypes.sizeof(entry),
            ctypes.byref(returned),
            None,
            None,
        )
        != 0
    ):
        raise ctypes.WinError(winsock.WSAGetLastError())
    if not entry.value or returned.value != ctypes.sizeof(entry):
        raise OSError("Winsock returned an invalid ConnectEx entry point")


class _SocketBoundConnectEx:
    def connect(self, conn: socket.socket, address: Any) -> Any:
        if conn.type == socket.SOCK_STREAM:
            _prepare_connectex(conn)
        return super().connect(conn, address)


def new_event_loop() -> asyncio.AbstractEventLoop:
    if sys.platform != "win32":
        return asyncio.new_event_loop()
    from asyncio.windows_events import IocpProactor, ProactorEventLoop

    class SocketBoundProactor(_SocketBoundConnectEx, IocpProactor):
        pass

    return ProactorEventLoop(proactor=SocketBoundProactor())


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=new_event_loop)
    return asyncio.run(coro)
