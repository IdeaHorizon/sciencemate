"""交给 worker 的 socket 地址必须是**绝对路径**（2026-08-19 node20 实测事故）。

## 出了什么事

配置默认值是相对路径 `data/harness_sockets`。App Server 的 cwd 是
`platform/backend`，worker 的 cwd 是 harness 根 —— **同一个相对路径在两个进程
里指向两个不同的文件**：

    worker 绑 → <harness根>/data/harness_sockets/s-xxx.sock
    后端连 →   <harness根>/platform/backend/data/harness_sockets/s-xxx.sock

后端连不上，按"drain 换代"的设计回退到管道；而新 worker 只读 socket ——
**死锁**。两个进程各自都"成功"了，谁都不报错，新会话永远停在"正在启动"。

## 判据（2026-08-21 收紧）

第一版的修法是"把算出来的地址 resolve 成绝对路径"，并且明写「配置写成相对
路径是合法的（部署脚本里到处都是）」。那是**只修了症状**：resolve 相对路径
用的仍然是 cwd，所以它保证的只是"这个进程内前后一致"，跨进程该分叉还是分叉。
同一个病根两天后在 Session worktree 上原样复发了一次（43 个会话全部失联）。

现在的判据是：**相对的根根本不是一个位置**，它在配置层就被拒绝
（`app.config.data_root`），不给它机会去"解析成某个东西"。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import DataRootError, settings
from pathlib import Path as _P
from app.services.harness_sessions import _control_address as _control_socket_path

HARNESS_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _real_harness(monkeypatch):
    monkeypatch.setattr(settings, "harness_root", str(HARNESS_ROOT))
    monkeypatch.setattr(settings, "harness_control_socket", True)
    from app.services.harness_contract import _harness_root

    _harness_root.cache_clear()
    yield
    _harness_root.cache_clear()


@pytest.mark.parametrize(
    "configured",
    ["data/harness_sockets", "./sockets", "var/run/harness"],
)
def test_a_relative_root_is_refused_outright(monkeypatch, configured):
    """相对的根不是一个位置 —— 不许"解析"，直接拒。

    这条曾经断言的是相反的事（"相对是合法的，只要交出去的绝对就行"）。
    改判据的理由见模块 docstring：解析用的是 cwd，两个进程就是两个答案。
    """
    monkeypatch.setattr(settings, "harness_socket_root", configured)
    with pytest.raises(DataRootError) as raised:
        _control_socket_path("p1", "s1")
    assert configured in str(raised.value), "报错必须把那个非法值原样报出来"


def test_the_address_does_not_depend_on_the_current_directory(monkeypatch, tmp_path):
    """**这就是那个事故**：两个进程 cwd 不同，同一个配置解析出两个文件。

    合法配置（绝对根）下，地址在任何 cwd 下都必须一致。
    """
    root = tmp_path / "d"
    monkeypatch.setattr(settings, "harness_socket_root", str(root))
    original = Path.cwd()
    try:
        os.chdir(tmp_path)
        from_a = _control_socket_path("p1", "s1")
        deeper = tmp_path / "platform" / "backend"
        deeper.mkdir(parents=True)
        os.chdir(deeper)
        from_b = _control_socket_path("p1", "s1")
    finally:
        os.chdir(original)
    assert from_a == from_b, (
        f"同一个 session 在不同 cwd 下算出两个地址：{from_a} vs {from_b} —— "
        "worker 绑一个、后端连另一个，两边都不报错"
    )


def test_an_absolute_short_config_is_left_alone(monkeypatch):
    """短的绝对根 → 原样使用。

    根**必须够短**：pytest 的 tmp_path 在 macOS 上本身就接近 AF_UNIX 上限，
    用它当根会触发长度回退（那是正确行为，但测的就不是这条了）。
    """
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="sockroot-", dir="/tmp"))
    try:
        monkeypatch.setattr(settings, "harness_socket_root", str(root))
        raw = _control_socket_path("p1", "s1")
        assert raw is not None
        path = _P(raw)
        assert path.is_absolute()
        # macOS 的 /tmp 是 /private/tmp 的软链，而我们**故意** resolve 了 ——
        # 比较也要 resolve，否则测的是软链不是逻辑。
        assert path.parent == root.resolve()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_switching_the_feature_off_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "harness_control_socket", False)
    assert _control_socket_path("p1", "s1") is None
