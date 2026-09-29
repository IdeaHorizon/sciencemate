"""后端和 harness 的默认数据根必须逐字一致。

后端（`app.config._default_data_root`）在导入期就要这个值，那时 core 不一定在
sys.path 上，所以两侧各留了一份 per-OS 逻辑。这份测试钉住它们不许分叉 —— 否则
Windows 上后端会用 `%LOCALAPPDATA%\afs`、worker 会退回 `~/.harness-framework`，
就是 08-21 丢 43 个会话那种「两边都不报错的分叉」。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.services.harness_imports import ensure_harness_importable

ensure_harness_importable()
from core import paths  # noqa: E402


@pytest.mark.parametrize("platform,localappdata", [
    ("linux", None),
    ("darwin", None),
    ("win32", r"C:\Users\u\AppData\Local"),
    ("win32", None),  # LOCALAPPDATA 缺失时的回退
])
def test_backend_and_harness_defaults_are_identical(monkeypatch, platform, localappdata):
    monkeypatch.setattr(paths.sys, "platform", platform)
    monkeypatch.setattr(config.sys, "platform", platform)
    if localappdata is None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    else:
        monkeypatch.setenv("LOCALAPPDATA", localappdata)
    home = Path(r"C:\Users\u") if platform == "win32" else Path("/home/u")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert config._default_data_root() == paths.default_home()


def test_windows_default_lands_under_localappdata(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    assert config._default_data_root() == Path(r"C:\Users\u\AppData\Local") / "afs"
