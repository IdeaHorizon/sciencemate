"""组织服务器要让同事连上，就得绑 0.0.0.0 —— launcher 从前只会绑 127.0.0.1。

## 病例

`install-server.sh` 装出来的服务器只听本机：`app.launcher` 写死
`uvicorn.run(host="127.0.0.1")`，没有 `--host`。装完印的地址也是 127.0.0.1。
同事拿到它连不上，而屏幕上一路都是 ✅。node20 之所以能用，是因为有人在前面
架了一层 nginx 把 192.0.2.20:18080 转到 127.0.0.1:18081 —— 那是一台机器的
运维知识，不是产品路径。

## 两条判据

1. `--host` 真的传到了 uvicorn（不是加了个参数没人读）。
2. 探端口探的是**将要绑的那个地址**：同一台机器上 nginx 可能只占着
   `<局域网IP>:18080`，探 127.0.0.1 说"空的"，真绑 0.0.0.0 才撞上 ——
   判据得落在真要做的那件事上。
"""
from __future__ import annotations

import socket
import sys
import types

import pytest

from app import launcher


def test_the_host_reaches_uvicorn(monkeypatch, tmp_path) -> None:
    served: dict = {}
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kwargs: served.update({"app": app, **kwargs})
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(launcher, "prepare_the_environment", lambda: (None, None))
    monkeypatch.setattr(launcher, "pick_a_free_port",
                        lambda preferred=None, host="127.0.0.1": 18555)

    assert launcher.main(["start", "--host", "0.0.0.0", "--no-browser"]) == 0
    assert served["host"] == "0.0.0.0", served


def test_the_default_is_still_this_machine_only(monkeypatch, tmp_path) -> None:
    """桌面版不该因为这个改动突然对局域网开门。"""
    served: dict = {}
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kwargs: served.update(kwargs)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(launcher, "prepare_the_environment", lambda: (None, None))
    monkeypatch.setattr(launcher, "pick_a_free_port",
                        lambda preferred=None, host="127.0.0.1": 18556)

    assert launcher.main(["start", "--no-browser"]) == 0
    assert served["host"] == "127.0.0.1"


def test_the_probe_answers_the_same_as_a_real_bind_on_that_host() -> None:
    """探的是将要绑的那个地址，而且答案要和真绑一致。

    夹具：把一个端口只绑在某个非 loopback 地址上（真机上是 nginx 占着
    192.0.2.20:18080）。对 127.0.0.1 探它必须是空的。对 0.0.0.0 探它的答案
    **随平台而异** —— Linux 上一个 LISTEN 中的具体地址会让通配绑定 EADDRINUSE；
    macOS 上带 SO_REUSEADDR 的通配绑定却能成功（uvicorn 自己也是这么绑的，
    所以那台服务器真会起来）。判据因此不是"必须撞上"，而是**探针说的和真绑
    一样**：说空就真能绑，说占就真绑不上。第一版写成"必须抛"，在 macOS 上红了
    —— 红得对，它把平台的事实当成了缺陷（[[criterion-must-not-depend-on-the-environment]]）。
    """
    lan = next((a for a in _ipv4_addresses() if not a.startswith("127.")), None)
    if lan is None:
        pytest.skip("这台机器没有非 loopback 的 IPv4 地址，摆不出这个局面")
    holder = socket.socket()
    try:
        holder.bind((lan, 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        # 只看 loopback，它就是空的。
        assert launcher.pick_a_free_port(port, host="127.0.0.1") == port

        # 真绑一次 0.0.0.0（和 uvicorn 同样的 SO_REUSEADDR），记下平台的真实答案。
        real = socket.socket()
        real.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            real.bind(("0.0.0.0", port))
            real.listen(1)
            really_free = True
        except OSError:
            really_free = False
        finally:
            real.close()

        if really_free:
            assert launcher.pick_a_free_port(port, host="0.0.0.0") == port
        else:
            with pytest.raises(OSError):
                launcher.pick_a_free_port(port, host="0.0.0.0")
    finally:
        holder.close()


def _ipv4_addresses() -> list[str]:
    seen: list[str] = []
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen
