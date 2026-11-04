"""Windows 原生程序的输出到模型手里是 UTF-8（#1263 诊断包）。

lujy 10-29 的现场：模型在受管 shell 里跑 ``wsl.exe`` / ``whoami.exe``，九条探针 stdout 逐字节相同、
全是 UTF-16LE 的「拒绝访问。/ Wsl/E_ACCESSDENIED」，模型拿 ``od -c`` 和 ``iconv`` 一条条试编码。
编码是起进程之前就确定的事实：咽喉在本机 Windows 后端上把它转成 UTF-8 再交出去。

``wsl.exe`` 那两段是 11-04 在 9800x3d 上直接抓的原始字节（英文 Windows，OEM 850 / ANSI 1252）；
CP936 那几段按 lujy 诊断包里的原文编码（中文 Windows 的 OEM 与 ANSI 都是 936）。
"""
from __future__ import annotations

import asyncio
import inspect
import sys

import pytest

from shared.lib.native_output import code_pages, to_utf8

# 9800x3d, `wsl.exe -l -q`
WSL_LIST = bytes.fromhex("5500620075006e00740075002d00320034002e00300034000d000a00")
# 9800x3d, `wsl.exe -d NoSuchDistro true`
WSL_NOT_FOUND = bytes.fromhex(
    "5400680065007200650020006900730020006e006f00200064006900730074007200690062007500740069006f00"
    "6e00200077006900740068002000740068006500200073007500700070006c0069006500640020006e0061006d00"
    "65002e000d000a004500720072006f007200200063006f00640065003a002000570073006c002f00530065007200"
    "76006900630065002f00570053004c005f0045005f00440049005300540052004f005f004e004f0054005f004600"
    "4f0055004e0044000d000a00")
CN = ("cp936",)


def test_wsl_exe_speaks_utf16le() -> None:
    assert to_utf8(WSL_LIST).decode("utf-8") == "Ubuntu-24.04\r\n"
    text = to_utf8(WSL_NOT_FOUND).decode("utf-8")
    assert text.startswith("There is no distribution with the supplied name.")
    assert "Wsl/Service/WSL_E_DISTRO_NOT_FOUND" in text


def test_lujys_access_denied_reads_as_chinese() -> None:
    raw = "拒绝访问。\r\nWsl/E_ACCESSDENIED\r\n".encode("utf-16-le")
    assert to_utf8(raw, CN).decode("utf-8") == "拒绝访问。\r\nWsl/E_ACCESSDENIED\r\n"


def test_chinese_utf16_with_a_zero_low_byte() -> None:
    """「一」是 U+4E00（``00 4E``）：低位的 0 不能让整段被当成别的东西。"""
    raw = "参数错误：第一个参数无效。\r\nError code: Wsl/E_INVALIDARG\r\n".encode("utf-16-le")
    assert to_utf8(raw, CN).decode("utf-8") == "参数错误：第一个参数无效。\r\nError code: Wsl/E_INVALIDARG\r\n"


def test_chinese_utf16_line_inside_a_mixed_stream() -> None:
    raw = b"probe:\n" + "拒绝访问。\r\nWsl/E_ACCESSDENIED\r\n".encode("utf-16-le")
    assert to_utf8(raw, CN).decode("utf-8") == "probe:\n拒绝访问。\r\nWsl/E_ACCESSDENIED\r\n"


def test_console_programs_speak_the_oem_code_page() -> None:
    raw = "用户信息\r\n----------------\r\n\r\n用户名       SID\r\n".encode("cp936")
    assert to_utf8(raw, CN).decode("utf-8") == "用户信息\r\n----------------\r\n\r\n用户名       SID\r\n"


def test_utf8_lines_are_not_touched_when_mixed_with_code_page_lines() -> None:
    """一条命令里 Python 打 UTF-8、whoami 打 CP936：按行各解各的，UTF-8 那行不能被当成 CP936。"""
    raw = "结果：完成\n".encode("utf-8") + "用户名\r\n".encode("cp936")
    assert to_utf8(raw, CN).decode("utf-8") == "结果：完成\n用户名\r\n"


def test_utf16_after_utf8_in_one_stream() -> None:
    """``echo x; wsl.exe -l -q`` —— 前半是 UTF-8，后半是 UTF-16LE。"""
    assert to_utf8(b"distros:\n" + WSL_LIST, CN).decode("utf-8") == "distros:\nUbuntu-24.04\r\n"


def test_an_even_length_utf8_prefix_is_not_swallowed_by_utf16() -> None:
    """11-04 9800x3d 实测：``echo 'python says: ✓ 完成'; wsl.exe --status`` —— 前缀恰好 24 字节（偶数），
    整段一次判定会把它和后面的 UTF-16 一起解成「祰桴湯猠…」。"""
    prefix = "python says: ✓ 完成\n".encode("utf-8")
    assert len(prefix) % 2 == 0
    raw = prefix + "Access is denied. \r\nError code: Wsl/E_ACCESSDENIED\r\n".encode("utf-16-le")
    assert to_utf8(raw, ("cp850", "cp1252")).decode("utf-8") == (
        "python says: ✓ 完成\nAccess is denied. \r\nError code: Wsl/E_ACCESSDENIED\r\n")


def test_utf8_comes_back_as_the_same_bytes() -> None:
    raw = "图 1：收敛曲线 ✓\n".encode("utf-8")
    assert to_utf8(raw, CN) is raw


def test_a_tail_cut_inside_a_character_still_decodes() -> None:
    """咽喉交出去的是文件尾部，开头可能是半个双字节字符 / 半个 UTF-16 码元。"""
    assert to_utf8("访问被拒绝\r\n".encode("cp936")[1:], CN).decode("utf-8").endswith("问被拒绝\r\n")
    assert to_utf8(WSL_NOT_FOUND[1:]).decode("utf-8").endswith("WSL_E_DISTRO_NOT_FOUND\r\n")


def test_undecodable_bytes_are_left_for_the_caller() -> None:
    raw = b"\xff\xfe\xfd binary"
    assert to_utf8(raw, ()) == raw


def test_code_pages_are_a_windows_fact() -> None:
    if sys.platform != "win32":
        assert code_pages() == ()
    else:
        assert all(page.startswith("cp") and page != "cp65001" for page in code_pages())


def test_only_the_native_windows_backend_transcodes() -> None:
    """WSL 目标里是 Linux 程序（本来就是 UTF-8）；macOS / Linux 上非 UTF-8 多半是二进制。"""
    from shared.lib import cancellable_subprocess

    source = inspect.getsource(cancellable_subprocess.spawn_and_wait)
    assert 'windows_native = getattr(backend, "name", "") == "win32"' in source
    assert "if windows_native:" in source and "to_utf8(_out_b), to_utf8(_err_b)" in source


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 原生后端上转码")
def test_through_the_throat_on_windows(tmp_path) -> None:
    from core import isolation
    from shared.lib.cancellable_subprocess import spawn_and_wait

    try:
        isolation.select_backend()
    except Exception:
        pytest.skip("这台 Windows 没有可用的原生隔离后端")

    class _State:
        kill_event = None

        def append_transcript(self, *_a, **_k) -> None:
            pass

    script = ("import sys; sys.stdout.buffer.write('Ubuntu-24.04\\r\\n'.encode('utf-16-le')); "
              "sys.stderr.buffer.write('拒绝访问。\\r\\n'.encode('utf-16-le') + "
              "'Wsl/E_ACCESSDENIED\\r\\n'.encode('utf-16-le'))")
    status, rc, out, err = asyncio.run(spawn_and_wait(
        sys.executable, "-c", script, state=_State(), timeout=60, cwd=str(tmp_path),
        writable_roots=[tmp_path]))
    assert (status, rc) == ("done", 0), err
    assert out.decode("utf-8") == "Ubuntu-24.04\r\n"
    assert err.decode("utf-8") == "拒绝访问。\r\nWsl/E_ACCESSDENIED\r\n"
