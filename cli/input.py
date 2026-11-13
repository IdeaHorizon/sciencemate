"""Terminal input, multiline paste and terminal state management."""

from __future__ import annotations

import asyncio
import sys

import chat as runtime

from . import commands, terminal


def enable_line_editing() -> None:
    """Activate readline only when starting the terminal UI."""
    try:
        import readline
    except ImportError:
        return
    try:
        readline.parse_and_bind("set enable-bracketed-paste on")
    except Exception:
        pass


_PASTE_COALESCE_WINDOW_S = 0.05


def _stdin_more_buffered(timeout: float = _PASTE_COALESCE_WINDOW_S) -> bool:
    """stdin 里是否还有紧跟着的、已缓冲的输入（粘贴续行）。

    非 POSIX / stdin 不是可 select 的 fd（管道重定向、Windows）→ 返 False，
    退化成逐行读（旧行为），不影响正确性只是不合并粘贴。
    """
    try:
        import select

        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(r)
    except Exception:
        return False


def _coalesce_pasted_lines(first_line, read_next, more_available) -> str:
    """把一次多行粘贴合并成单条输入。纯逻辑，便于单测。

    first_line：已读到的第一行。
    more_available()：是否还有紧跟着的已缓冲续行（真实现用 select 判 stdin）。
    read_next()：读下一行（真实现用 input("")）；EOF/读不到 → 停。
    """
    lines = [first_line]
    while more_available():
        try:
            nxt = read_next()
        except EOFError:
            break
        if nxt is None:
            break
        lines.append(nxt)
    text = "\n".join(lines)
    return runtime._BRACKETED_PASTE_RE.sub("", text)


def _stdin_listener(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str]) -> None:
    """Daemon 线程：阻塞读 stdin，通过 call_soon_threadsafe 投递到 asyncio queue。

    为什么是 daemon thread 而不是 asyncio.to_thread(input)：
      to_thread 把 input() 扔进 default executor 的非 daemon 线程；当主程序
      要退出时 stdin_task.cancel() 只能在下一次 await 点生效，executor 线程
      仍阻塞在 input() 系统调用上没法被打断。解释器退出时要等所有非 daemon
      线程 join → user 必须按一下回车让 input() 返回线程才退出，体感就是
      "跑完了还得回车"。
    Daemon 线程在解释器退出时被强制终止，不会等 stdin，直接回 shell。

    提示符**由本线程通过 input(prompt) 显示**（不再由主协程单独 print）——这样
    readline 知道提示符宽度，退格不会把 `你 ›` 一起抹掉（见 _USER_PROMPT_RL）。
    多行粘贴由 _coalesce_pasted_lines 合并成单条输入（续行用空提示符读，不重画
    `你 ›`）。
    """
    while True:
        try:
            line = input(terminal._USER_PROMPT_RL)
        except (EOFError, KeyboardInterrupt):
            loop.call_soon_threadsafe(queue.put_nowait, "/exit")
            return
        line = _coalesce_pasted_lines(
            line,
            read_next=lambda: input(""),
            more_available=_stdin_more_buffered,
        )
        loop.call_soon_threadsafe(queue.put_nowait, line)
        # 退出命令：投递后**立刻 return**，不要再进下一次 input()（#141）。
        # 否则线程会重新阻塞在 readline 里持有 raw TTY，进程退出时被强杀、
        # 来不及复原终端 —— termios 快照兜底之外再堵上这个源头。
        if line.strip() in commands._EXIT_COMMANDS:
            return
