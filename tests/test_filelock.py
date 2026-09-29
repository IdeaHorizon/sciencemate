"""`shared.lib.filelock`：跨进程文件锁的契约，两个平台同一份测试。

真起第二个进程去抢 —— 同进程里两个 handle 的互斥在 POSIX 上碰巧也成立，但那
不是这个模块要回答的问题（个人版里后端与 worker 是两个进程）。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from shared.lib import filelock

ROOT = Path(__file__).resolve().parents[1]


def _holder(lock_path: Path) -> subprocess.Popen:
    """另一个进程：拿住锁、报「held」、直到 stdin 关闭才放。"""
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from shared.lib import filelock
        with filelock.exclusive({str(lock_path)!r}):
            print("held", flush=True)
            sys.stdin.read()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


def _release(proc: subprocess.Popen) -> None:
    proc.stdin.close()
    proc.wait(timeout=10)


def test_a_lock_held_by_another_process_is_seen_as_held(tmp_path):
    lock = tmp_path / "x.lock"
    holder = _holder(lock)
    try:
        with pytest.raises(filelock.LockHeld):
            with filelock.exclusive(lock, blocking=False):
                pass
        assert filelock.try_hold(lock) is None
    finally:
        _release(holder)
    # 对方放了就拿得到
    with filelock.exclusive(lock, blocking=False):
        pass


def test_blocking_acquire_waits_for_the_other_process(tmp_path):
    import threading
    import time

    lock = tmp_path / "x.lock"
    holder = _holder(lock)
    got_at: list[float] = []

    def take():
        with filelock.exclusive(lock):
            got_at.append(time.monotonic())

    t = threading.Thread(target=take)
    t.start()
    time.sleep(0.3)
    assert not got_at, "对方还持有着，阻塞式 acquire 不该已经拿到"
    released_at = time.monotonic()
    _release(holder)
    t.join(timeout=10)
    assert got_at and got_at[0] >= released_at


def test_the_handle_returned_by_try_hold_is_the_lock(tmp_path):
    lock = tmp_path / "x.lock"
    fh = filelock.try_hold(lock)
    assert fh is not None
    assert filelock.try_hold(lock) is None, "同一把锁第二次 try_hold 必须拿不到"
    fh.close()  # handle 关了锁就没了
    again = filelock.try_hold(lock)
    assert again is not None
    again.close()


def test_the_lock_file_content_stays_readable_while_held(tmp_path):
    """两处调用方把持有者信息写进锁文件给下一个来抢的人读（chat 单实例、worker
    注册表行）。Windows 的 msvcrt.locking 是强制锁 —— 锁的字节必须在内容区之外，
    否则这条契约在 Windows 上静默失效。"""
    lock = tmp_path / "x.lock"
    fh = filelock.try_hold(lock)
    assert fh is not None
    fh.write('{"pid": 1234}')
    fh.flush()
    try:
        assert lock.read_text(encoding="utf-8") == '{"pid": 1234}'
        holder = subprocess.run(
            [sys.executable, "-c", f"print(open({str(lock)!r}).read())"],
            capture_output=True, text=True, timeout=10,
        )
        assert holder.stdout.strip() == '{"pid": 1234}', holder.stderr
    finally:
        fh.close()


def test_exclusive_creates_the_parent_directory(tmp_path):
    lock = tmp_path / "deep" / "er" / "x.lock"
    with filelock.exclusive(lock):
        assert lock.exists()
