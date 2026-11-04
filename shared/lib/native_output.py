"""Windows 原生程序的输出转成 UTF-8 —— 在咽喉里做一次，不让模型去猜编码。

## 为什么（2026-10-29 lujy 诊断包，#1263）

Windows 0.5.8 上，模型在受管 shell 里跑 ``whoami.exe`` / ``wsl.exe`` / ``cmd.exe``，看到的是乱码或
一串空：中文 Windows 上控制台程序按 OEM 代码页（CP936）写字节，``wsl.exe`` 自己的话是
UTF-16LE，而咽喉之后的每个调用方都按 UTF-8 解。模型于是用 ``od -c`` 和 ``iconv`` 一条条试编码，
同一个探针跑了九遍。编码是**进程起来之前就确定的事实**（这台机器的代码页、这个程序的约定），
不该由模型探测。

## 怎么转

只在本机 Windows 后端（``win32``）的模型命令上做（WSL 目标里是 Linux 程序，本来就是 UTF-8；
macOS / Linux 上非 UTF-8 的字节多半是二进制，按 CP936 解只会把替换字符换成假汉字）：

逐行（按 ``\\n`` 切）各解各的，因为一条命令的输出常是混排的（``echo`` 一句 UTF-8，再跑一个
``wsl.exe``）。整段一次判定试过，会把偶数长度的 UTF-8 前缀当成 UTF-16 一起解成乱码（11-04 9800x3d 实测）：

1. 行首是 ``\\0``（上一行的 UTF-16 换行 ``\\n\\0`` 切剩的半个）或行里的 0 字节对位落在码元高位
   （``\\r\\0`` 就是）→ 按 UTF-16LE 严格解；
2. 本来就是合法 UTF-8 的行原样不动（Python / Git for Windows 的输出）；
3. 剩下的依次试 OEM 代码页、ANSI 代码页（严格解码，解不开就原样留给调用方按 UTF-8 替换）。

转的只是交给调用方的那段尾巴；落盘保留的完整输出文件仍是原始字节。
"""
from __future__ import annotations

import codecs
import sys
from functools import lru_cache

__all__ = ["code_pages", "to_utf8"]


@lru_cache(maxsize=1)
def code_pages() -> tuple[str, ...]:
    """这台 Windows 的 OEM、ANSI 代码页（去重、去掉 UTF-8 与 Python 不认识的）。非 Windows → 空。"""
    if sys.platform != "win32":
        return ()
    import ctypes

    found: list[str] = []
    for getter in ("GetOEMCP", "GetACP"):
        try:
            page = int(getattr(ctypes.windll.kernel32, getter)())  # type: ignore[attr-defined]
        except Exception:
            continue
        if page == 65001:
            continue
        name = f"cp{page}"
        try:
            codecs.lookup(name)
        except LookupError:
            continue
        if name not in found:
            found.append(name)
    return tuple(found)


def _utf16_shaped(chunk: bytes) -> bool:
    """按对位认 UTF-16LE：偶数长度，0 字节落在码元高位（奇数下标）的不少于落在低位的，且至少一个。

    比例判据会把「UTF-8 前缀 + 一段 UTF-16」错位一个字节读成一串汉字（ASCII 的两两组合
    正好落在 CJK 区），对位判据不会：错位之后 0 字节全跑到低位上去了。低位允许有 0 ——
    「一」是 U+4E00，写出来就是 ``00 4E``。普通文本行里根本没有 0 字节，所以这条也不会误伤
    UTF-8 / 代码页的行；最后还要严格解得开才算。
    """
    if len(chunk) < 2 or len(chunk) % 2:
        return False
    high, low = chunk[1::2].count(0), chunk[0::2].count(0)
    return high > 0 and high >= low


def _utf16(chunk: bytes) -> str | None:
    """按 UTF-16LE 严格解；形状不对或解不开 → None。"""
    if chunk.startswith(b"\xff\xfe") and len(chunk) % 2 == 0:
        body = chunk[2:]
    elif _utf16_shaped(chunk):
        body = chunk
    else:
        return None
    try:
        return body.decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def _line(line: bytes, pages: tuple[str, ...]) -> bytes:
    if line[:1] == b"\x00":
        # 上一行是 UTF-16：它的换行 ``\\n\\0`` 被按 ``\\n`` 切开，``\\0`` 留在了这一行开头 ——
        # 这一行也是 UTF-16（中文为主的行可以一个 0 字节都没有，靠的就是这个记号）。
        body = line[1:]
        if not body:
            return b""
        if len(body) % 2 == 0:
            try:
                return body.decode("utf-16-le").encode("utf-8")
            except UnicodeDecodeError:
                pass
    if b"\x00" in line:
        text = _utf16(line)
        if text is not None:
            return text.encode("utf-8")
    try:
        line.decode("utf-8")
        return line
    except UnicodeDecodeError:
        pass
    for page in pages:
        for start in (0, 1):            # 尾巴可能从一个双字节字符的后半个开始
            try:
                return line[start:].decode(page).encode("utf-8")
            except UnicodeDecodeError:
                continue
    return line


def to_utf8(data: bytes, pages: tuple[str, ...] | None = None) -> bytes:
    """把一段 Windows 原生程序的输出转成 UTF-8 字节。已经是 UTF-8 的原样返回。"""
    if not data:
        return data
    pages = code_pages() if pages is None else pages
    if b"\x00" not in data:
        try:
            data.decode("utf-8")
            return data
        except UnicodeDecodeError:
            pass
    pieces = data.split(b"\n")
    return b"\n".join(_line(piece, pages) for piece in pieces)
