"""这台电脑的系统语言 —— 用户在操作系统里排好的那张偏好列表，原样、按先后。

## 为什么后端读，不让网页自己读 `navigator.languages`

界面第一次打开、这个人还没选过语言时，得替他定一次：系统语言列表里有中文就中文，
否则英文（wangd 2026-10-19）。答这一问要的是**整张列表** —— 很多中文研究者的系统
是英文，中文排在第二位。

而桌面版的界面跑在 WKWebView 里，它只交出列表的**第一项**：2026-10-19 在一台
`AppleLanguages = (en-GB, zh-Hans-CN)` 的 Mac 上实测，同样配置的 .app 里
`navigator.languages` 是 `["en-GB"]` —— 后面那个中文看不见，这台机器会被判成英文。

桌面版的后端就跑在这个人自己的电脑上，读得到整张表。所以它读、前端判（判据只写在
前端 `languageForASystem` 一处）。组织服务器不在这个人的电脑上，它的系统语言与他
无关 —— 那里由装配层回答"不知道"（空列表），前端退回浏览器给的那一项。

## 失败方向

读不到就是空列表，不抛：这只是替人猜一个起点，猜不了就由前端用浏览器那一项，界面上
那个用目标语言写的切换钮照样在。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

#: `defaults read -g AppleLanguages` 的输出里每一项是一行带引号或不带引号的语言标签。
_APPLE_LANGUAGE = re.compile(r'^\s*"?([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*)"?,?\s*$')


def preferred_languages() -> list[str]:
    """这台电脑的系统语言，按用户排的先后（BCP 47 写法，如 `zh-Hans-CN`、`en-GB`）。"""
    try:
        if sys.platform == "darwin":
            return _macos()
        if sys.platform == "win32":
            return _windows()
        return _from_environment(os.environ)
    except Exception:  # noqa: BLE001 - 猜不了就不猜，见模块说明「失败方向」
        return []


#: Windows 安装器上人自己点过的语言，安装器写在数据根下（`Speech.TellTheApp`）。没点过就没有这个文件。
INSTALL_CHOICE_FILE = "install-language"


def language_chosen_at_install(data_root) -> str | None:
    """安装器上人自己选过的语言（"zh" / "en"）—— 没选过、读不到、写着别的都是 None。

    默认的英文不算选择：安装器只在人点过「中文 / English」（或命令行点名）时才写这一笔，
    所以这里读到的一定是人自己选的。
    """
    try:
        value = (Path(data_root) / INSTALL_CHOICE_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if value in ("zh", "en") else None


def parse_apple_languages(text: str) -> list[str]:
    """`defaults read -g AppleLanguages` 打印的是一个 plist 数组：

        (
            "en-GB",
            "zh-Hans-CN"
        )
    """
    return [m.group(1) for line in text.splitlines() if (m := _APPLE_LANGUAGE.match(line))]


def _macos() -> list[str]:
    # 绝对路径：打包后后端进程的 PATH 由启动器拼，不保证有 /usr/bin。
    done = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                          capture_output=True, text=True, timeout=5, check=False)
    return parse_apple_languages(done.stdout) if done.returncode == 0 else []


def _windows() -> list[str]:
    """显示语言 + 用户的语言列表，按这个先后、去重。

    只看显示语言不够：2026-10-19 在 9800x3d 上实测，显示语言是 `en-GB, en-US`，
    「设置 → 时间和语言」里那张语言列表（装中文输入法也在这）是 `zh-Hans-CN` ——
    一个用英文界面的中文用户。`GetUserPreferredUILanguages` 只给前者。后者在
    `HKCU\\Control Panel\\International\\User Profile` 的 `Languages`（`Get-WinUserLanguageList`
    读的就是它）。
    """
    found = _windows_display_languages()
    for tag in _windows_language_list():
        if tag not in found:
            found.append(tag)
    return found


def _windows_language_list() -> list[str]:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International\User Profile") as key:
            value, _kind = winreg.QueryValueEx(key, "Languages")
    except OSError:
        return []
    return [tag for tag in value if isinstance(tag, str) and tag] if isinstance(value, list) else []


def _windows_display_languages() -> list[str]:
    import ctypes
    from ctypes import wintypes

    mui_language_name = 0x8
    call = ctypes.WinDLL("kernel32", use_last_error=True).GetUserPreferredUILanguages
    call.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.ULONG),
                     ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]
    call.restype = wintypes.BOOL
    count, size = wintypes.ULONG(0), wintypes.ULONG(0)
    if not call(mui_language_name, ctypes.byref(count), None, ctypes.byref(size)):
        return []
    buffer = ctypes.create_unicode_buffer(size.value)
    if not call(mui_language_name, ctypes.byref(count), ctypes.cast(buffer, ctypes.c_void_p),
                ctypes.byref(size)):
        return []
    # 结果是一串以 NUL 分隔、以两个 NUL 结尾的名字：`zh-CN\0en-US\0\0`。
    return [name for name in buffer[:size.value].split("\0") if name]


def _from_environment(environ) -> list[str]:
    """Linux / 其他：`LANGUAGE` 是有先后的列表，其余几个各给一项（`zh_CN.UTF-8` → `zh-CN`）。"""
    found: list[str] = []
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for item in (environ.get(name) or "").split(":"):
            tag = item.split(".")[0].split("@")[0].replace("_", "-")
            if tag and tag not in ("C", "POSIX") and tag not in found:
                found.append(tag)
    return found
