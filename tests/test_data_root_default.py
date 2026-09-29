"""默认数据根「在哪」—— per-OS，一处回答，且后端/harness 两侧不许分叉。

后端（`app.config`）在导入期就要这个值，那时 core 不一定在 sys.path 上，所以两侧各有
一份 per-OS 逻辑。**这份测试钉住它们逐字一致** —— 否则就是 08-21 丢 43 个会话那种：
后端把项目写进 A、worker 把 KB/记忆写进 B，两边都不报错。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core import paths


def test_posix_default_is_the_dotdir(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.default_home() == Path("/home/u/.harness-framework")


def test_windows_default_is_under_localappdata(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    assert paths.default_home() == Path(r"C:\Users\u\AppData\Local") / "afs"


def test_windows_default_falls_back_when_localappdata_missing(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(r"C:\Users\u")))
    # 在 POSIX 上跑 Windows 路径逻辑：反斜杠是字面字符，用与代码相同的 join 构造期望值
    assert paths.default_home() == Path(r"C:\Users\u") / "AppData" / "Local" / "afs"


def test_home_prefers_the_env_over_the_default(monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", "/data/explicit")
    assert paths.home() == Path("/data/explicit")


def test_home_uses_the_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("HARNESS_FRAMEWORK_HOME", raising=False)
    assert paths.home() == paths.default_home()

# 后端/harness 两侧默认值逐字一致的对账，在两侧都可 import 的后端套件里：
# platform/backend/tests/test_data_root_agreement.py。
