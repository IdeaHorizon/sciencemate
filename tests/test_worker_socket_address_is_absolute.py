"""worker 的命令 socket 地址必须是绝对路径，且长度判据基于绝对路径。

## 现场（2026-08-19，socket 通道落地后第一个真实部署）

`harness_socket_root` 默认是相对路径 `data/harness_sockets`。而 App Server 与
worker 的 **cwd 不同** —— 前者在 `platform/backend`，后者在 HARNESS_ROOT，这正是
部署配方（`run-local.sh`）的样子。于是同一个 `data/harness_sockets/s-xxx.sock`
指向两个位置：worker 绑 A、App Server 连 B。

后果不是报错，是**等到控制超时**（900 秒），期间两端没有任何一处说"你们说的
不是同一个文件"。研究 run 停在 `stale_unknown`。

更隐蔽的是它顺带废掉了长度回退：长度检查看到的是相对路径的 44 字节（没超），
各自绝对化之后却是 116 —— 该回退的没回退，绑的一侧侥幸成功、连的一侧
`AF_UNIX path too long`。

## 为什么这个 bug 能活到现在

既有部署跑的是 socket 改造**之前**的代码，走 stdio 老路；而 stdio 不关心 cwd。
新通道的第一个真实使用者就是第一个受害者。单测全绿 —— 因为测试传的都是
tmp_path 那样的绝对路径。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from core.worker_addressing import (
    MAX_UNIX_SOCKET_PATH,
    control_socket_path,
    relocated_if_too_long,
    socket_name,
)


class TestAddressIsAlwaysAbsolute:
    def test_a_relative_root_yields_an_absolute_address(self):
        """两个 cwd 不同的进程必须得到**同一个字符串**。"""
        path = control_socket_path("data/harness_sockets", "proj", "sess")
        assert path.is_absolute()

    def test_relocation_also_absolutises(self):
        """worker 侧拿到相对地址时同样要绝对化，否则它绑的还是自己 cwd 下那个。"""
        assert relocated_if_too_long("data/harness_sockets/s-x.sock").is_absolute()

    def test_the_same_relative_root_resolves_identically_regardless_of_caller(self):
        """同一个相对根，两次调用得到同一个绝对地址 —— 这是两端能碰头的前提。"""
        first = control_socket_path("data/harness_sockets", "p", "s")
        second = control_socket_path("data/harness_sockets", "p", "s")
        assert first == second


class TestLengthIsJudgedOnTheResolvedPath:
    def test_a_short_absolute_root_is_kept(self):
        """根够短就原地放 —— 回退是兜底，不是默认行为。

        刻意不用 pytest 的 `tmp_path`：它本身就有 100+ 字符（
        `/private/var/folders/…/pytest-of-…/pytest-N/test_name0`），一定触发
        回退，于是这条测试会验成它的反面。
        """
        root = Path(tempfile.gettempdir()) / "hf-sock-test"
        path = control_socket_path(root, "proj", "sess")
        assert path.parent == root.resolve()
        assert len(str(path)) <= MAX_UNIX_SOCKET_PATH

    def test_a_deep_root_falls_back_to_the_temp_dir(self):
        """部署路径深不该让平台起不来 —— 挪走，真实地址以注册表行为准。"""
        deep = Path("/tmp") / ("x" * 60) / ("y" * 60)
        path = control_socket_path(deep, "proj", "sess")
        assert str(path).startswith(tempfile.gettempdir())
        assert len(str(path)) <= MAX_UNIX_SOCKET_PATH

    def test_a_relative_root_that_resolves_too_long_falls_back(self, monkeypatch, tmp_path):
        """本次事故的精确形态：相对路径量着不长，绝对化之后超限。

        没有这条，长度回退会被相对路径"骗过"——绑的一侧侥幸成功、连的一侧
        AF_UNIX path too long。
        """
        deep_cwd = tmp_path / ("d" * 40) / ("e" * 40) / ("f" * 40)
        deep_cwd.mkdir(parents=True)
        monkeypatch.chdir(deep_cwd)
        path = control_socket_path("data/harness_sockets", "proj", "sess")
        assert len(str(path)) <= MAX_UNIX_SOCKET_PATH
        assert str(path).startswith(tempfile.gettempdir())

    def test_every_produced_address_fits_the_kernel_limit(self):
        """无论根长什么样，产出的地址都必须绑得上 —— 这是本模块的全部职责。"""
        for root in ("data/harness_sockets", "/tmp/short", "/tmp/" + "z" * 200, "~/sockets"):
            assert len(str(control_socket_path(root, "p", "s"))) <= MAX_UNIX_SOCKET_PATH


def test_identity_is_independent_of_address():
    """身份（project/session）与地址是两回事：地址可能被挪，名字不会变。"""
    name = socket_name("proj", "sess")
    for root in ("data/harness_sockets", "/tmp/x", "/tmp/" + "z" * 200):
        assert control_socket_path(root, "proj", "sess").name == name
