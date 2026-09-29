"""worker 的**地址**：命令 socket 放哪（RFC 异步运行时 P0-2）。

App Server 生成地址、经环境变量交给 worker；worker 绑上它，并把**实际**
地址写进注册表行。两个进程必须对同一条规则达成一致，所以规则只有这一份
（`harness_contract` 那条既定的共享桥），不在两边各抄一遍 —— 抄件会各自
演化，而分叉时两边都不报错。

身份（project/session）与地址是两回事：地址可以因为长度限制被挪走，
身份不会变。发现地址的正规路径始终是注册表行的 `control_socket`。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

#: AF_UNIX 的 sun_path 硬上限：macOS 104、Linux 108。取保守值并留余量 ——
#: 超一个字节的后果不是降级，是 bind 当场失败、这个会话根本起不来。
MAX_UNIX_SOCKET_PATH = 100

#: 命令面传输。默认 AF_UNIX（POSIX：文件权限 0600 就是认证）；**Windows 上
#: CPython 没有 AF_UNIX**（`socket.AF_UNIX` 不存在、`asyncio.open_unix_connection`
#: 也不存在），只能走 TCP 环回 + spawn-token 握手。可用 `HARNESS_CONTROL_TRANSPORT`
#: 显式指定（`tcp` 让 POSIX 也能跑同一套 TCP 测试）。
TRANSPORT_UNIX = "unix"
TRANSPORT_TCP = "tcp"
TCP_LOOPBACK_HOST = "127.0.0.1"



def default_transport() -> str:
    forced = os.environ.get("HARNESS_CONTROL_TRANSPORT", "").strip().lower()
    if forced in (TRANSPORT_UNIX, TRANSPORT_TCP):
        return forced
    return TRANSPORT_TCP if sys.platform == "win32" else TRANSPORT_UNIX


def socket_name(project_id: str, session_id: str) -> str:
    digest = hashlib.sha256(f"{project_id}::{session_id}".encode()).hexdigest()[:16]
    return f"s-{digest}.sock"


def control_socket_path(runtime_root: Path | str, project_id: str, session_id: str) -> Path:
    """这个 session 的命令 socket 地址。

    ## 为什么不放 session state 目录

    session state 的真实路径长这样：

        …/project_worktrees/<uuid>/<uuid>/.research/cache/runtime/runs/
        orchestrator__<uuid>__session__<uuid>/worker.sock          → 284 字节

    实测 bind 直接 `AF_UNIX path too long`。所以短根 + 哈希名。

    ## 地址必须是绝对路径

    相对路径在这里是**静默的错**：App Server 与 worker 的 cwd 不同（前者在
    `platform/backend`，后者在 HARNESS_ROOT —— 这正是部署配方的样子），同一个
    `data/harness_sockets/s-xxx.sock` 于是指向两个位置。worker 绑 A、App Server
    连 B，连不上就一直等到控制超时（900s），而两端都没有任何一处报错说"你们
    说的不是同一个文件"。

    更隐蔽的是它**顺带废掉了下面那条长度回退**：长度检查看到的是相对路径的
    长度（44 字节，没超），可各自绝对化之后是 116 —— 于是该回退的没回退，
    绑的那一侧侥幸成功、连的那一侧 `AF_UNIX path too long`。

    2026-08-19 实测：这是新 socket 通道落地后第一个 cwd≠HARNESS_ROOT 的真实
    部署，两个缺陷同时现形。

    ## 超长时回退，不是报错

    配置的根本身也可能太深（部署路径不归我们决定）。这时回退到系统临时目录：
    为"部署路径深了一点"让整个平台起不来，是把一件本可以自己解决的事变成
    事故。真实地址以注册表行为准，所以挪走是安全的。
    """
    name = socket_name(project_id, session_id)
    # 先绝对化再量长度：两个进程必须得到同一个字符串，且长度判据要基于它。
    preferred = Path(runtime_root).expanduser().resolve() / name
    if len(str(preferred)) <= MAX_UNIX_SOCKET_PATH:
        return preferred
    return Path(tempfile.gettempdir()) / "harness-control" / name


def control_address(
    runtime_root: Path | str,
    project_id: str,
    session_id: str,
    transport: str | None = None,
) -> str:
    """这个 session 的命令面地址，**带 scheme**：``unix:<绝对路径>`` 或
    ``tcp:127.0.0.1:<具体端口>``。

    ## 地址由**要去连的那一方**定下来，两种传输一个形状

    unix 那边本来就是这样：地址从身份算出来，两个进程各自算一遍得到同一个字符串，
    谁也不用告诉谁。TCP 曾经不是 —— 交出去的是 `:0`（内核挑端口），于是「端口是几号」
    只有 worker 知道，后端得靠别的通道问回来。那条通道（注册表行）要等第一个请求才
    存在，而第一个请求要等连接：**Windows 上每次都死锁**（2026-09-10 真机，见 #926）。

    #926 当时的修法是再开一条汇报通道（worker 在 stdout 上报地址）。环是断了，可
    「谁告诉谁地址」从此有两个答案。**根因在更上一层**：让 worker 挑端口这件事本身
    没有必要 —— 后端完全可以先问内核要一个空闲端口，再把**具体地址**交下去。这样
    TCP 与 unix 结构完全一致，`is_ephemeral` / 等待汇报 / 汇报事件全部不需要存在。

    代价是一个窗口：拿到端口到 worker 绑上之间（真机实测 ~0.2 秒），端口理论上可能被
    别人抢走。**如实说清它现在是什么**：抢走了 worker 当场带着 `control_socket_unavailable`
    退出——**响亮地失败，不是静默挂着**，报错里带得出这个端口；下一次 spawn 自然拿到
    一个新端口。这里**没有**自动重试，别在文档里许一个代码给不出的东西。
    用这么一个响亮且自愈的小窗口，换掉一整套「谁告诉谁地址」的机制，值。

    注册表行保留它**唯一**的职责：没有 spawn 过这个 worker 的新后端怎么接回来。
    """
    if (transport or default_transport()) == TRANSPORT_TCP:
        return f"{TRANSPORT_TCP}:{TCP_LOOPBACK_HOST}:{pick_loopback_port()}"
    # unix：注意**不带** `unix:` 前缀 —— 老 worker（drain 换代）把这个环境变量
    # 当裸路径读，加了 scheme 它会拿去 bind 一个不存在的路径。裸路径 = unix，这条
    # 由 `parse_control_address` 认下来。
    return str(control_socket_path(runtime_root, project_id, session_id))


def pick_loopback_port() -> int:
    """问内核要一个此刻空闲的环回端口。

    绑 0 → 读端口 → 关掉。关掉之后到 worker 绑上之间有一个窗口，但抢占是**可恢复**的：
    worker 绑不上会带着 `control_socket_unavailable` 退出，调用方换个端口重来。
    不设 `SO_REUSEADDR` —— 在 Windows 上它的语义是"允许别人抢同一端口"，那正是这里
    要避免的。
    """
    import socket as _socket

    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as probe:
        probe.bind((TCP_LOOPBACK_HOST, 0))
        return int(probe.getsockname()[1])


def parse_control_address(address: str) -> tuple[str, object]:
    """把地址解析成 ``(transport, endpoint)``：
    ``tcp:host:port`` → ``("tcp", (host, port))``；``unix:X`` 或**裸路径** → ``("unix", Path)``。

    裸路径认成 unix 是**故意**的向后兼容：注册表行里的老地址、以及 POSIX 上经环境
    变量交给 worker 的裸路径，都不带 scheme。
    """
    text = str(address).strip()
    if text.startswith(f"{TRANSPORT_TCP}:"):
        _, _, hostport = text.partition(":")
        host, _, port = hostport.rpartition(":")
        return TRANSPORT_TCP, (host or TCP_LOOPBACK_HOST, int(port))
    if text.startswith(f"{TRANSPORT_UNIX}:"):
        return TRANSPORT_UNIX, Path(text[len(TRANSPORT_UNIX) + 1:])
    return TRANSPORT_UNIX, Path(text)


def relocated_if_too_long(path: Path | str) -> Path:
    """worker 侧：拿到的地址如果绑不上（太长），挪到临时目录同名处。

    worker 不重新推导身份 —— 它只是把**给定的**地址换个放得下的地方，
    并在注册表行里如实报告实际绑在哪。
    """
    # 同样先绝对化：worker 的 cwd 与 App Server 不同，相对地址在两边不是
    # 同一个文件，而长度判据也会因此看走眼。
    candidate = Path(path).expanduser().resolve()
    if len(str(candidate)) <= MAX_UNIX_SOCKET_PATH:
        return candidate
    return Path(tempfile.gettempdir()) / "harness-control" / candidate.name
