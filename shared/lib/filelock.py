"""跨进程文件锁 —— 一处回答，两个平台。

仓库里原本有七处各自 ``import fcntl`` 的内联锁（KB、记忆、向量索引、LLM 准入
槽位、chat 单实例、worker 注册表行、Project 仓库互斥），其中三处写着
「Windows 退化成 no-op」。那不是退化，是**静默数据竞争**：个人版里后端与 worker
是两个进程，都写同一份 KB / 记忆。本模块把它们收成一个答案，并把那三句谎话删掉。

语义（两平台对齐）：

* **排他、建议锁、跟 handle 走**：拿锁的是一个打开的文件对象；handle 关了、
  进程死了，锁自动没了（POSIX ``flock`` 天然如此；Windows 亦然）。
* **阻塞 / 非阻塞**：非阻塞拿不到抛 :class:`LockHeld`（``OSError`` 子类，调用方
  原来接 ``OSError`` 的写法照样成立）。
* **锁文件内容可读**：有两处把持有者信息（pid、注册表行）写进锁文件，给下一个来
  抢的人读。POSIX ``flock`` 不碰内容；Windows ``msvcrt.locking`` 是**强制锁** ——
  锁住的字节别的 handle 读都会被拒。所以 Windows 侧锁的是文件末尾之外的一个远端
  字节（``_WIN_LOCK_OFFSET``），内容区永远可读。

Windows 的 ``LK_LOCK`` 只重试十次（约 10 秒）就抛，不是真阻塞；这里用非阻塞 +
轮询实现阻塞语义，节拍 ``_POLL_S``。
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from typing import IO, Iterator

from shared.lib.filesystem import io_path

__all__ = ["LockHeld", "acquire", "release", "exclusive", "try_hold"]


class LockHeld(OSError):
    """非阻塞拿锁失败：别的 handle 正持有。"""


_POLL_S = 0.05


def _fd(handle: IO | int) -> int:
    return handle if isinstance(handle, int) else handle.fileno()


if sys.platform == "win32":  # pragma: no cover - 由 tests/win32/ 在真机上覆盖
    import msvcrt

    #: 锁的字节放在内容永远到不了的地方：内容区（几百字节的 pid / JSON 行）
    #: 对其他进程保持可读。锁定区域允许超出文件末尾。
    _WIN_LOCK_OFFSET = 0x7FFF_0000

    def _lock_now(fd: int) -> bool:
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        finally:
            os.lseek(fd, pos, os.SEEK_SET)

    def _unlock(fd: int) -> None:
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass  # 没锁着就没锁着：释放是幂等的
        finally:
            os.lseek(fd, pos, os.SEEK_SET)

    def acquire(handle: IO | int, *, blocking: bool = True) -> None:
        fd = _fd(handle)
        while not _lock_now(fd):
            if not blocking:
                raise LockHeld("file lock is held by another process")
            time.sleep(_POLL_S)

    def release(handle: IO | int) -> None:
        _unlock(_fd(handle))

else:
    import fcntl

    def acquire(handle: IO | int, *, blocking: bool = True) -> None:
        op = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(_fd(handle), op)
        except OSError as exc:
            if blocking:
                raise
            raise LockHeld("file lock is held by another process") from exc

    def release(handle: IO | int) -> None:
        fcntl.flock(_fd(handle), fcntl.LOCK_UN)


@contextmanager
def exclusive(path: os.PathLike | str, *, blocking: bool = True) -> Iterator[IO]:
    """开（建）锁文件、上排他锁、yield 那个 handle、解锁并关文件。

    父目录顺手建好 —— 七处调用方全都先 mkdir 再开锁，收到这里。
    """
    p = os.fspath(io_path(path))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "a+", encoding="utf-8") as fh:
        acquire(fh, blocking=blocking)
        try:
            yield fh
        finally:
            release(fh)


def try_hold(path: os.PathLike | str, *, mode: int = 0o600) -> IO | None:
    """非阻塞拿锁；拿到就把**持有着锁的 handle** 交给调用方（调用方要一直留着引用，
    handle 被回收锁就没了）；拿不到返回 None。

    给「进程活着就一直持有」的场景用：chat 单实例、worker 注册表行、LLM 准入槽位。
    文件按 ``mode`` 建，内容由调用方决定写什么给下一个来抢的人看。
    """
    p = os.fspath(io_path(path))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    fh = os.fdopen(os.open(p, os.O_RDWR | os.O_CREAT, mode), "r+", encoding="utf-8")
    try:
        acquire(fh, blocking=False)
    except LockHeld:
        fh.close()
        return None
    return fh
