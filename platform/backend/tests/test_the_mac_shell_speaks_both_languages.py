"""Mac 壳说给人听的话，两种语言都写。

wangd 2026-10-19：「对话框中英两行并列」。壳起不来时界面还没出来，它不知道这个人要哪种
语言 —— 失败页、下载失败的提示框中英两段并列（`Speech.both` / `Speech.pair`）；菜单、
面板标题、按钮放不下两行，按这台 Mac 的系统语言（`Speech.t`，同应用第一次打开的判据）。

判据扫的是"`Shell.swift` 里出现了中文"这件事本身：每个带中文的字面量都必须是
`Speech.t` / `Speech.both` / `Speech.pair` 的中文那个参数，英文参数里不许有中文；写日志的
`NSLog(...)` 整句放行（那不是界面）。注释不算。
"""
from __future__ import annotations

import re
from pathlib import Path

SHELL = Path(__file__).resolve().parents[3] / "platform" / "desktop" / "mac" / "Shell.swift"
HAN = re.compile(r"[\u4e00-\u9fff]")


def _skip_interpolation(src: str, i: int) -> int:
    """`\\(` 之后，跳到配对的 `)` 之后。里面是代码，可能又有字符串。"""
    depth = 1
    while i < len(src):
        if src[i] == '"':
            i = _skip_string(src, i)
            continue
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _skip_string(src: str, i: int) -> int:
    """从开引号起，跳到字符串结束之后（认 `\"\"\"` 多行、`\\(` 插值、转义）。"""
    multiline = src.startswith('"""', i)
    j = i + (3 if multiline else 1)
    while j < len(src):
        if src.startswith("\\(", j):
            j = _skip_interpolation(src, j + 2)
            continue
        if src[j] == "\\":
            j += 2
            continue
        if multiline and src.startswith('"""', j):
            return j + 3
        if not multiline and (src[j] == '"' or src[j] == "\n"):
            return j + 1
        j += 1
    return j


def _strings_and_mask(src: str) -> tuple[list[tuple[int, int]], str]:
    spans: list[tuple[int, int]] = []
    masked = list(src)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if masked[k] != "\n":
                masked[k] = " "

    i = 0
    while i < len(src):
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = len(src) if j < 0 else j
            blank(i, j)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = len(src) if j < 0 else j + 2
            blank(i, j)
            i = j
        elif src[i] == '"':
            j = _skip_string(src, i)
            spans.append((i, j))
            blank(i, j)
            i = j
        else:
            i += 1
    return spans, "".join(masked)


def _arguments(masked: str, opened: int) -> list[tuple[int, int]]:
    depth, start, out = 0, opened + 1, []
    for k in range(opened, len(masked)):
        ch = masked[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                out.append((start, k))
                return out
        elif ch == "," and depth == 1:
            out.append((start, k))
            start = k + 1
    raise AssertionError("括号没配上")


def _offenders(src: str) -> tuple[list[str], int]:
    spans, masked = _strings_and_mask(src)
    allowed: list[tuple[int, int]] = []
    offenders: list[str] = []
    calls = 0
    for call in re.finditer(r"\bNSLog\s*\(", masked):
        args = _arguments(masked, call.end() - 1)
        allowed.append((args[0][0], args[-1][1]))   # 写日志：整句放行
    for call in re.finditer(r"\bSpeech\.(t|both|pair)\s*\(", masked):
        calls += 1
        args = _arguments(masked, call.end() - 1)
        if len(args) < 2:
            offenders.append(f"Shell.swift:{src.count(chr(10), 0, call.start()) + 1} Speech.{call.group(1)} 少一个参数")
            continue
        allowed.append(args[0])
        a, b = args[1]
        if HAN.search(src[a:b]):
            offenders.append(f"Shell.swift:{src.count(chr(10), 0, a) + 1} 英文那一侧写着中文")
    for a, b in spans:
        if HAN.search(src[a:b]) and not any(x <= a and b <= y for x, y in allowed):
            offenders.append(f"Shell.swift:{src.count(chr(10), 0, a) + 1} → {src[a:b][:60]}")
    return offenders, calls


def test_every_chinese_line_in_the_mac_shell_also_says_it_in_english() -> None:
    offenders, calls = _offenders(SHELL.read_text(encoding="utf-8"))
    assert calls >= 15, f"只扫到 {calls} 处 Speech 调用 —— 扫法塌了，这道闸是空的"
    assert not offenders, (
        f"Mac 壳里这些话只有中文（{len(offenders)} 处）。对话框用 Speech.both / Speech.pair，"
        "菜单与按钮用 Speech.t：\n" + "\n".join(offenders))


def test_the_scan_sees_a_bare_chinese_line() -> None:
    """判据自己的反例：一句裸中文、英文侧写中文、插值里的字符串 —— 都得被它看见。"""
    sample = (
        'let a = "裸着的中文"\n'
        'let b = Speech.t("撤销", "撤销")\n'
        'let c = Speech.t("好", "OK")\n'
        'NSLog("日志里的中文 %@", "\\(x)")\n'
        'let d = Speech.both("起不来：\\(f("里面"))", "Could not start: \\(f("inner"))")\n'
        'let e = """\n多行里的中文\n"""\n'
    )
    offenders, _ = _offenders(sample)
    lines = {int(o.split(":")[1].split()[0]) for o in offenders}
    assert lines == {1, 2, 6}, offenders   # 日志、写对了的、插值里的字符串都不报


def test_menus_follow_the_whole_system_language_list() -> None:
    """菜单按系统语言：读整张 `Locale.preferredLanguages`（网页只拿得到第一项）。"""
    src = SHELL.read_text(encoding="utf-8")
    speech = src[src.index("enum Speech {"):src.index("struct Words {")]
    assert "Locale.preferredLanguages.contains" in speech, "没按整张系统语言列表判"
    assert "preferredLocalizations" not in speech, (
        "Bundle.preferredLocalizations 只在 app 声明过的本地化里挑 —— 壳一个都没声明，永远是英文")
