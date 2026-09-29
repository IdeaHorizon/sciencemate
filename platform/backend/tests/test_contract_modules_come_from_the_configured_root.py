"""契约模块必须来自**配置的** harness 根，不是 `sys.modules` 里碰巧那个。

现场（2026-08-19，xdist 把测试打散到同一个 worker 之后必现）：一条造假
harness 根的测试先跑，`worker_addressing()` 把假根塞进 sys.path 并 import 了
`core` —— `sys.modules['core']` 从此被假的占住。同进程里后面每一次
`from core import worker_addressing` 都是 ImportError → 契约"不可用" →
`_control_socket_path` 静默返回 None → 后端回退 stdio，而 worker 只读 socket。

**那就是 node20 死锁的形状**，而这个模块正是为消除它而存在的。串行时只是
顺序碰巧对，所以三年没人看见。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.config import settings
from app.services.harness_contract import (
    HarnessContractUnavailable,
    _harness_root,
    worker_addressing,
)

HARNESS_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def real_root(monkeypatch):
    monkeypatch.setattr(settings, "harness_root", str(HARNESS_ROOT))
    _harness_root.cache_clear()
    yield HARNESS_ROOT
    _harness_root.cache_clear()


def _plant_a_fake_core(tmp_path: Path) -> Path:
    """造一个只有空 `core/` 的假根，并让它先占住 sys.modules。"""
    fake = tmp_path / "fake-harness"
    (fake / "core").mkdir(parents=True)
    (fake / "core" / "__init__.py").write_text("", encoding="utf-8")
    sys.path.insert(0, str(fake))
    for name in [n for n in list(sys.modules) if n == "core" or n.startswith("core.")]:
        del sys.modules[name]
    import core  # noqa: PLC0415  —— 故意把假的装进 sys.modules
    assert Path(core.__file__).parent.parent == fake
    return fake


def test_a_foreign_core_does_not_poison_the_contract(real_root, tmp_path, monkeypatch):
    fake = _plant_a_fake_core(tmp_path)
    try:
        module = worker_addressing()
    finally:
        while str(fake) in sys.path:
            sys.path.remove(str(fake))
        for name in [n for n in list(sys.modules) if n == "core" or n.startswith("core.")]:
            del sys.modules[name]
    assert real_root in Path(module.__file__).resolve().parents, (
        "契约模块必须来自配置的 harness 根 —— 拿到别处那个同名模块，"
        "两个进程就各按各的格式来了"
    )
    assert hasattr(module, "control_socket_path")


def test_a_root_without_the_contract_fails_loud_not_silent(monkeypatch, tmp_path):
    """根本身不是合法 checkout 时要抛，不是悄悄给个来路不明的模块。"""
    bogus = tmp_path / "not-a-checkout"
    bogus.mkdir()
    monkeypatch.setattr(settings, "harness_root", str(bogus))
    _harness_root.cache_clear()
    try:
        with pytest.raises(HarnessContractUnavailable):
            worker_addressing()
    finally:
        _harness_root.cache_clear()
