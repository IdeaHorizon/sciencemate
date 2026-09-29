"""个人档没配 PLATFORM_DATA_ROOT 时的默认数据根 —— 一份规则，两个读者。

`app.config` 在导入那一刻就要它；`app.launcher` 在**导入 config 之前**也要它
（自更新的载荷指针住在数据根里，而 launcher 得先切换载荷、再让 config 拍快照）。
两边都从这里读，就没有第二份「数据在哪」——2026-09-07 五份抄件分叉丢会话的那种
形状（#834）不许再长回来。

这套 per-OS 逻辑与 harness 侧 ``core.paths.default_home()`` **逐字一致**，由
`tests/test_data_root_default.py` 钉住不许分叉。这里不 import 任何 app 模块。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def default_data_root() -> Path:
    """POSIX `~/.harness-framework`；Windows `%LOCALAPPDATA%\\afs`。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "afs"
    return Path.home() / ".harness-framework"


def data_root_before_config() -> Path:
    """`PLATFORM_DATA_ROOT` > `HARNESS_FRAMEWORK_HOME` > 默认 —— 给 launcher 在
    config 之前用的那一问，**与 `config.Settings` 个人档那条规则逐字相同**。

    原来这里只看 `PLATFORM_DATA_ROOT`。而 config 在个人档下是
    `PLATFORM_DATA_ROOT or HARNESS_FRAMEWORK_HOME or 默认`。于是只配了
    `HARNESS_FRAMEWORK_HOME`（DELIVERY.md 里写着的那个变量）的那台机器上，两处
    分叉：**暂存**更新走 config 的根（新目录），**应用**更新走这里的根（默认目录）。

    后果不是报错，是 **自更新永远装不上**：POST /update/install 说 staged 成功，
    重启，`GET /update` 还是 `installed=旧版 staged=新版`，再点一次还是这样。
    2026-09-16 打 0.5.0 做自更新真机验时撞上（测试脚本只设了 HARNESS_FRAMEWORK_HOME）。

    「两个变量都配且指着不同目录就起不来」那道闸拦不住这一条 —— 它问的是"两个都
    配了吗"，而这里的病是**只配了一个**，然后两处对"另一个没配时算什么"给了不同答案。
    """
    explicit = os.environ.get("PLATFORM_DATA_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    harness_home = os.environ.get("HARNESS_FRAMEWORK_HOME", "").strip()
    if harness_home:
        return Path(harness_home).expanduser()
    return default_data_root()
