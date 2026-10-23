"""linux 后端：Landlock / bwrap 守写边界，bwrap 断网，systemd 的 cgroup 守资源。

三样东西各自探测、各自记账，谁在就用谁：

* **bwrap**（需要 user namespace）：``--ro-bind / /`` 全盘只读 + 按层挂回可写；
  ``--ro-bind`` 压住 ``.git``；``--unshare-net`` 断网。有它就同时有
  WRITE_BOUNDARY / GIT_UNWRITABLE / NET_DENY。
* **Landlock**（内核 ≥ 5.13，不需要 userns）：bwrap 起不来时的写墙主力（CI runner、
  node20、HPC 作业里 userns 常被禁 —— node20 就是 ``apparmor_restrict_unprivileged_userns=1``）。
  纯放行清单：``.git`` 不靠它挡，靠**结构判据**（可写根不含 gitdir / 指针文件，含了就在
  ``prepare`` 拒绝派发，见 ``_native.git_paths_inside_writable``）→ GIT_UNWRITABLE。
  **断网两层**：Landlock ABI ≥ 4（内核 ≥ 6.7）拒 TCP bind/connect；启动器再装一层 seccomp
  禁建 AF_INET / AF_INET6 socket（UDP、DNS、老内核上的 TCP 一起断，unix socket 不动，#1180）。
  探针真连一次、真建一个 UDP socket，被拒才声明 NET_DENY，记账 ``net_scope: inet``（两层都在）
  / ``tcp``（只有 Landlock 那层）；bwrap 整个网络命名空间才是 ``all``。
* **systemd-run --user --scope**：MemoryMax（+ ``MemorySwapMax=0``）/ TasksMax / CPUQuota，
  也就是 cgroup v2。只设 MemoryMax 的话，有大 swap 的机器（node20：160 GB swapfile）上超限的
  内存只会被换出、不触发 OOM —— 内存墙拦不住失控命令，它慢慢换页直到墙钟超时（#1207）。
  声明了上限就当场 OOM 并记原因，不靠换页"慢慢跑完"。
  探针成功才声明 MEM_CAP / PIDS_CAP；每条命令的 scope 放进**它自己的 slice**
  （``app-hf<id>.slice``），cleanup 时 ``systemctl --user stop`` 那个 slice（连同 scope），
  比 PID 快照差集更准（``setsid`` 逃逸也逃不出 cgroup）。为什么要多一层 slice 见
  :func:`_memory_wall`：OOM 记在 cgroup 的 ``memory.events`` 里，而 scope 的 cgroup
  在命令一结束就被 systemd 收走了（#1179）。

层次（外→内）：systemd-run → bwrap → landlock 启动器 → 命令。有 bwrap 时不走 landlock
启动器，最内层换成 ``_exec.py`` 垫片 —— 最内层必须是我们的代码，载荷起不起得来才有人说
（``core.isolation.LAUNCHER_ERROR_MARKER``）。
"""

from __future__ import annotations

import functools
import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from . import CommandSpec, Invariant, IsolationContractError
from ._native import (
    NativeLaunch,
    WriteLayers,
    canonical,
    exec_shim_argv,
    git_paths_inside_writable,
    payload_environment,
    private_scratch,
    run_ok,
    write_layers,
)

_LANDLOCK_EXEC = Path(__file__).with_name("_landlock_exec.py")


def _probe_bwrap() -> tuple[str | None, str]:
    exe = shutil.which("bwrap")
    if not exe:
        return None, "bwrap not installed"
    with tempfile.TemporaryDirectory(prefix="hf-bwrap-probe-") as tmp:
        target = Path(tmp).resolve() / "probe"
        if not run_ok([exe, "--die-with-parent", "--ro-bind", "/", "/", "--dev-bind", "/dev",
                       "/dev", "--unshare-net", "/bin/true"]):
            return None, "bwrap cannot create namespaces here (userns disabled?)"
        if run_ok([exe, "--die-with-parent", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev",
                   "/bin/sh", "-c", f"echo x > '{target}'"]) or target.exists():
            return None, "bwrap did not deny a write under --ro-bind /"
    return exe, ""


#: 网络探针：连本机一个真在听的端口。被拒 = EACCES/EPERM → 0；连上 → 4；别的 → 3。
#: 目标必须是**真在听**的端口 —— 127.0.0.1 上没人听时不加沙箱也 ECONNREFUSED，
#: 分不出真假。
#: ``socket()`` 本身也在 try 里：seccomp 那一层在**建**socket 时就 EPERM（#1180），Landlock 那一层
#: 在 connect 时 EACCES —— 两种都是"被拒"，哪一步拒的不是这个探针要分的事。
_PROBE_CONNECT = (
    "import errno, socket, sys\n"
    "try:\n    s = socket.socket(); s.settimeout(3)\n"
    "    s.connect(('127.0.0.1', int(sys.argv[1])))\n"
    "except PermissionError:\n    sys.exit(0)\n"
    "except OSError as e:\n"
    "    sys.exit(0 if e.errno in (errno.EACCES, errno.EPERM) else 3)\n"
    "sys.exit(4)\n"
)
#: 建一个 UDP socket：被拒 → 0，建成 → 4，其它 → 3。Landlock 管不到它，seccomp 才管（#1180）。
#: 不用连，建得出就是漏 —— UDP 没有连接，DNS 查询就是这么发出去的。
_PROBE_UDP = (
    "import errno, socket, sys\n"
    "try:\n    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).close()\n"
    "except PermissionError:\n    sys.exit(0)\n"
    "except OSError as e:\n"
    "    sys.exit(0 if e.errno in (errno.EACCES, errno.EPERM) else 3)\n"
    "sys.exit(4)\n"
)


def _landlock_argv(roots: list[str], *, network: bool) -> list[str]:
    return [sys.executable, "-I", "-B", str(_LANDLOCK_EXEC),
            "--network", "allow" if network else "deny", json.dumps(roots), "--"]


def _probe_landlock() -> tuple[int, bool, str, str]:
    """→ (abi, 网络是否真拒, 口径, 原因)。abi=0 = 写墙不可用。

    口径 ``inet`` / ``tcp`` / ``none`` 是**探出来的**，不是按内核版本推的：
    ``--network deny`` 下真连一次本机在听的端口（TCP）、真建一个 UDP socket。TCP 被拒才算
    守到（Landlock ABI ≥ 4，或 seccomp 把 AF_INET socket 整个禁了 —— 老内核上也行，#1180）；
    UDP 也建不出来才是 ``inet``，建得出就只有 ``tcp``（seccomp 没装上：架构不认识 / 内核不给）。
    两个探针都带**对照**：同一段代码在 ``allow`` 下必须连得上 / 建得出。只测 deny 的话，
    监听器没起来 / 端口错了也会"被拒"，那是假阳性（[[feedback_baseline_can_vanish]]）。
    """
    if not _LANDLOCK_EXEC.exists():
        return 0, False, "none", "landlock launcher missing"
    import socket
    import subprocess

    try:
        out = subprocess.run([sys.executable, "-I", "-B", str(_LANDLOCK_EXEC), "abi"],
                             capture_output=True, text=True, timeout=10, check=False)
        abi = int((out.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0, False, "none", "landlock abi probe failed"
    if abi < 1:
        return 0, False, "none", "kernel has no Landlock (need >= 5.13, or seccomp blocks it)"
    with tempfile.TemporaryDirectory(prefix="hf-landlock-probe-") as tmp:
        allowed = Path(tmp).resolve() / "allowed"
        allowed.mkdir()
        outside = Path(tmp).resolve() / "outside"
        roots = [str(allowed), "/dev"]
        if run_ok([*_landlock_argv(roots, network=True), "/bin/sh", "-c",
                   f"echo x > '{outside}'"]) or outside.exists():
            return 0, False, "none", "landlock did not deny a write outside the allowed root"
        if not run_ok([*_landlock_argv(roots, network=True), "/bin/sh", "-c",
                       f"echo x > '{allowed / 'ok'}'"]):
            return 0, False, "none", "landlock denied an allowed write"
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = str(listener.getsockname()[1])
            tcp = [sys.executable, "-I", "-B", "-c", _PROBE_CONNECT, port]
            udp = [sys.executable, "-I", "-B", "-c", _PROBE_UDP]

            def _rc(argv: list[str]) -> int:
                return subprocess.run(argv, capture_output=True, timeout=15, check=False).returncode

            if _rc([*_landlock_argv(roots, network=True), *tcp]) != 4:
                return abi, False, "none", (
                    "landlock network probe has no control (allow could not connect)")
            if _rc([*_landlock_argv(roots, network=False), *tcp]) != 0:
                why = ("landlock abi>=4 present but did not deny a TCP connect" if abi >= 4
                       else f"landlock abi {abi} has no network rules and seccomp did not deny inet sockets")
                return abi, False, "none", why
            if _rc([*_landlock_argv(roots, network=True), *udp]) != 4:
                # TCP 已经证明拒得住；UDP 的对照建不出来说明这台机器连 allow 都建不了 UDP，
                # 不能据此宣称 inet 口径 —— 如实退回 tcp。
                return abi, True, "tcp", ""
            scope = "inet" if _rc([*_landlock_argv(roots, network=False), *udp]) == 0 else "tcp"
        except (OSError, subprocess.TimeoutExpired):
            return abi, False, "none", "landlock network probe failed"
        finally:
            listener.close()
    return abi, True, scope, ""


def _systemd_run_argv(exe: str, ident: str) -> tuple[list[str], str, str]:
    """命令进 systemd 的那一段 argv（不含限额与载荷）+ scope 名 + slice 名。探针与 prepare
    共用这一份：探针证明的必须正是 prepare 用的形状。

    * ``--slice``：scope 住进它自己的 slice。命令结束 scope 的 cgroup 立刻被收走，
      slice 留着、它的 ``memory.events`` 是层级计数 —— OOM 记在那里读得到（:func:`_memory_wall`）。
      名字 ``app-…`` = 嵌在 ``app.slice`` 里，与此前 scope 默认落的位置同层。
    * ``--collect``：被 OOM 停掉的 scope 进 failed 态，不 collect 就一直挂在用户 manager 里
      （``systemctl --user stop`` 不清 failed 态；node20 实测每次 OOM 留一个）。
    """
    unit = f"hf-{ident}.scope"
    slice_unit = f"app-hf{ident}.slice"
    argv = [exe, "--user", "--scope", "--quiet", "--collect", f"--unit={unit}",
            f"--slice={slice_unit}"]
    return argv, unit, slice_unit


#: cgroup v2 的挂载点。模块级是为了测试能指到一棵假 cgroup 树。
_CGROUP_ROOT = Path("/sys/fs/cgroup")


def _memory_wall(systemctl: str, slice_unit: str, memory_max: int,
                 returncode: int | None) -> tuple[str, str] | None:
    """这条命令是不是撞了内存墙：读它那片 slice 的 ``memory.events``（#1179）。

    **为什么读 slice 而不是 scope**（node20 实测，systemd 255 / 内核 6.8）：scope 的
    cgroup 在里面最后一个进程退出时由 systemd 自己删掉。asyncio ``proc.wait()`` 一返回就去读
    scope 的 ``memory.events``，8 次里 3 次文件已经没了（被 OOM 杀的 4 次里 2 次）—— 一个
    一半时间答不上来的判据。systemd 自己也读这个文件、把 OOM 记成 ``Result=oom-kill``，
    但 scope 有 OOMPolicy / 这条 Result 是 systemd 253 起的事（Debian 12 / RHEL 9 是 252、
    Ubuntu 22.04 是 249）。slice 在命令结束后照旧活着（我们自己在 cleanup 里停它），而 cgroup v2 的
    ``memory.events`` 是**层级**计数：子 cgroup 里的 OOM 记在每一层祖先上。slice 每条命令
    新建一个，计数起点就是 0。实测 5/5 读到 ``oom_kill 1``、对照 5/5 是 0。

    **只翻失败的命令**：退出码 0 的命令即使里面某个进程被 OOM 杀过，它也已经交了结果 ——
    不把成功改写成失败。失败 + cgroup 记过 OOM（``oom`` / ``oom_kill`` / ``oom_group_kill``
    任一 > 0）= 这次失败是撞了内存墙。

    答不上来（systemctl 不在 / slice 已经没了 / 文件读不了 / 挂载带 ``memory_localevents``
    让父层不计子层）一律 None —— 咽喉照旧交出原始退出码，不猜。
    """
    if not returncode:
        return None
    import subprocess

    try:
        out = subprocess.run(
            [systemctl, "--user", "show", "-p", "ControlGroup", "--value", slice_unit],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    cgroup = out.stdout.strip()
    if out.returncode != 0 or not cgroup.startswith("/"):
        return None
    try:
        text = (_CGROUP_ROOT / cgroup.lstrip("/") / "memory.events").read_text(encoding="utf-8")
    except OSError:
        return None
    counts: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            counts[key] = int(value.strip())
    keys = ("oom", "oom_kill", "oom_group_kill")
    if not any(counts.get(key, 0) > 0 for key in keys):
        return None
    detail = " ".join(f"{key}={counts.get(key, 0)}" for key in keys)
    return "memory", f"{detail} memory_max={memory_max}"


#: 探针跑的 cgroup 属性 —— 每条命令会设的每一种属性都必须在这里出现过：探针成功才证明
#: 这台机器的 systemd 吃得下它们（``MemorySwapMax`` 是 systemd 232 起的事）。
#: :func:`_cgroup_properties` 只许用这里出现过的属性名，`tests/test_the_memory_wall_has_no_swap_escape.py`
#: 钉着这一条。
_PROBE_PROPERTIES: tuple[str, ...] = (
    "MemoryMax=64M", "MemorySwapMax=0", "TasksMax=16", "CPUQuota=50%",
)


def _cgroup_properties(limits: Any) -> list[str]:
    """把 ``SandboxLimits`` 翻成 ``systemd-run -p`` 参数。

    内存上限成对出现：``MemoryMax`` 之外一律 ``MemorySwapMax=0``。否则有 swap 的机器上超限
    的内存被换出而不是被杀，"内存墙"退化成"慢慢换页直到墙钟超时"（#1207，node20 实测）。
    会被杀的是本来就超了声明的作业；它们该多申请内存，而不是靠 swap 隐瞒超支还拖慢别人。
    """
    props: list[str] = []
    memory = int(getattr(limits, "memory_bytes", 0) or 0)
    pids = int(getattr(limits, "pids", 0) or 0)
    cpus = float(getattr(limits, "cpus", 0) or 0)
    if memory:
        props += ["-p", f"MemoryMax={memory}", "-p", "MemorySwapMax=0"]
    if pids:
        props += ["-p", f"TasksMax={pids}"]
    if cpus:
        props += ["-p", f"CPUQuota={int(cpus * 100)}%"]
    return props


def _probe_systemd() -> tuple[str | None, str]:
    exe = shutil.which("systemd-run")
    if not exe:
        return None, "systemd-run not installed"
    argv, _unit, slice_unit = _systemd_run_argv(exe, f"probe{uuid.uuid4().hex[:8]}")
    probe_props = [arg for prop in _PROBE_PROPERTIES for arg in ("-p", prop)]
    ok = run_ok([*argv, *probe_props, "/bin/true"])
    systemctl = shutil.which("systemctl")
    if systemctl:
        run_ok([systemctl, "--user", "stop", slice_unit])
    if not ok:
        return None, "systemd-run --user --scope with cgroup limits failed (no user session / no delegation?)"
    return exe, ""


class LinuxBackend:
    name = "linux"

    def __init__(self) -> None:
        self._probed = False
        self._bwrap: str | None = None
        self._landlock = False
        self._landlock_abi = 0
        self._landlock_net = False
        #: 探出来的断网口径：inet（Landlock + seccomp，TCP/UDP/DNS 都断）| tcp | none。
        self._landlock_net_scope = "none"
        self._systemd: str | None = None
        self._reasons: dict[str, str] = {}
        #: 结构判据按可写根缓存：一条 run 的根是固定的，别每条命令都起一次 git。
        self._git_check_cache: dict[tuple[str, ...], tuple[tuple[Path, Path], ...]] = {}

    def _probe(self) -> None:
        if self._probed:
            return
        self._probed = True
        if not sys.platform.startswith("linux"):
            self._reasons["platform"] = f"not linux: {sys.platform}"
            return
        self._bwrap, why = _probe_bwrap()
        if why:
            self._reasons["bwrap"] = why
        self._landlock_abi, self._landlock_net, self._landlock_net_scope, why = _probe_landlock()
        self._landlock = self._landlock_abi >= 1
        if why:
            self._reasons["landlock"] = why
        elif self._landlock and not self._landlock_net:
            self._reasons["landlock"] = (
                f"landlock abi {self._landlock_abi} has no network rules "
                "(need >= 4, kernel >= 6.7) and seccomp did not deny inet sockets"
            )
        self._systemd, why = _probe_systemd()
        if why:
            self._reasons["systemd"] = why

    def capabilities(self) -> frozenset[Invariant]:
        self._probe()
        caps = {Invariant.WALLTIME, Invariant.GROUP_KILL}
        if self._bwrap or self._landlock:
            caps.add(Invariant.WRITE_BOUNDARY)
            # .git：bwrap 用 --ro-bind 压住；Landlock 用结构判据（可写根含 gitdir / 指针
            # 就在 prepare 拒绝派发）。两条路都是"守到"，只是一个靠挡、一个靠不接。
            caps.add(Invariant.GIT_UNWRITABLE)
        if self._bwrap:
            # 可写根内部的只读洞：bwrap 有 deny 原语（后挂的 --ro-bind 罩住前面的
            # --bind），按深度序发射就能兑现任意层数的嵌套。Landlock 是纯放行清单，
            # 只能靠补集覆盖，兑不兑现得起来是**逐条命令**的事 —— 那条挂在
            # NativeLaunch.unmet 上，不在这里冒充全局能力（#899-D / #900-B1）。
            caps.add(Invariant.READONLY_CARVEOUT)
        if self._bwrap or self._landlock_net:
            caps.add(Invariant.NET_DENY)
        if self._systemd:
            # 探针跑的正是 `_PROBE_PROPERTIES`（MemoryMax / MemorySwapMax / TasksMax /
            # CPUQuota）—— 它成功就意味着这几条 cgroup 限额这台机器都吃得下。CPU 此前没有名字，于是
            # sandbox_contract.cpus 没有任何东西能对上（#841）。
            caps.update({Invariant.MEM_CAP, Invariant.PIDS_CAP, Invariant.CPU_CAP})
        if Invariant.WRITE_BOUNDARY not in caps:
            return frozenset()
        return frozenset(caps)

    @property
    def pids_event_source(self) -> str | None:
        return "cgroup" if Invariant.PIDS_CAP in self.capabilities() else None

    @property
    def net_scope(self) -> str:
        """NET_DENY 的口径，进记账（``isolation_enforcement.net_scope``）。

        ``all`` = bwrap 整个网络命名空间；``inet`` = Landlock（TCP bind/connect）+ seccomp
        （禁建 AF_INET / AF_INET6 socket：UDP、DNS、老内核上的 TCP 一起断），unix socket 仍通；
        ``tcp`` = 只有 Landlock ABI 4 那一层，UDP / ICMP 不在内；``none`` = 没守到。
        哪一档是**探出来的**（:func:`_probe_landlock`），UI 原样带出，不许圆。
        """
        self._probe()
        if self._bwrap:
            return "all"
        if self._landlock_net:
            return self._landlock_net_scope
        return "none"

    @property
    def write_boundary_scope(self) -> str:
        """WRITE_BOUNDARY 的口径，进记账（``isolation_enforcement.write_boundary_scope``）。

        ``content_and_metadata`` = bwrap（``--ro-bind / /``：连 chmod 都是 EROFS）；
        ``content_only`` = Landlock —— 写内容与目录项挡得住，**元数据挡不住**。
        这不是实现写错，是 Landlock 没有这类访问位；内核文档明写
        chdir/stat/flock/chmod/chown/setxattr/utime/fcntl/access 这些动作限制不了。

        实测差别（#1095，可写根之外、当前用户拥有的文件）：

            chmod 600        bwrap rc=1 mode 不变    Landlock **rc=0，0644→0600**
            touch -m -d …    bwrap rc=1 mtime 不变   Landlock **rc=0，mtime 被改**
            os.setxattr      bwrap 失败              Landlock **成功**
            echo >> / truncate  两边都挡住

        能做的事到此为止：文件内容、代码、日志正文改不了；能做的是把当前用户
        拥有的任意文件 ``chmod 000``，让之后的 run 或服务读不了（可恢复的拒绝
        服务）。**最结构性的一条是记账** —— 两种后端此前声明同一个
        ``write_boundary``，strict 策略、UI 与节点看不出差别。口径分开之后，
        「这台机器的写墙含不含元数据」才是一个能被问出来的问题。
        """
        self._probe()
        if self._bwrap:
            return "content_and_metadata"
        if self._landlock:
            return "content_only"
        return "none"

    @property
    def unix_socket_reachable(self) -> bool | str:
        """宿主上的 path-based AF_UNIX socket 在墙内还够不够得到 —— 进记账。

        ``False`` = 够不到；``"partial"`` = socket 常驻地（/run、/var/run、/tmp）已换成
        空 tmpfs，别处的 socket 仍在；``True`` = 完全够得到。

        为什么要有这一项：Landlock 的 FS 规则族管 open/read/write/exec/unlink，**不管
        ``connect(2)``**；``--unshare-net`` 隔离的是 INET / abstract socket，path-based
        unix socket 是文件系统对象、照样穿过。于是"写边界罩住其余路径"对控制面 socket
        这一类文件失效，而 ``net_deny`` / ``write_boundary`` 的账面看起来像是盖住了它
        （#845）。盖不住就得有个地方说这句话。
        """
        self._probe()
        if self._bwrap:
            return "partial"
        return True

    @property
    def unavailable_reason(self) -> str:
        self._probe()
        return "; ".join(f"{k}: {v}" for k, v in self._reasons.items())

    def _git_paths_inside(self, own: tuple[Path, ...]) -> tuple[tuple[Path, Path], ...]:
        key = tuple(str(p) for p in own)
        cached = self._git_check_cache.get(key)
        if cached is None:
            cached = git_paths_inside_writable(own)
            self._git_check_cache[key] = cached
        return cached

    #: 宿主上 unix socket 的常驻地。断网的命令把这几处换成空 tmpfs —— path-based 的
    #: AF_UNIX ``connect(2)`` 既不归 Landlock 的 FS 规则族管、也不归 network namespace
    #: 管（它是文件系统对象），于是 DAC 允许时模型 payload 能和宿主上任何 unix 服务
    #: 对话：docker.sock、systemd 私有 socket、本地 MCP server……docker.sock 可达
    #: ≈ root 等价逃逸（#845）。tmpfs 一盖，这几处在命名空间里就是空的。
    _SOCKET_DIRS = ("/run", "/var/run", "/tmp")

    def _private_socket_dirs(self) -> list[str]:
        """能安全换成空 tmpfs 的 socket 常驻地（真目录、不是符号链接）。

        ``/var/run`` 在多数发行版上是指向 ``/run`` 的符号链接，对它 ``--tmpfs`` 没有
        意义（而且 bwrap 会报错）—— 只挑真目录。
        """
        out: list[str] = []
        for raw in self._SOCKET_DIRS:
            path = Path(raw)
            try:
                if path.is_dir() and not path.is_symlink():
                    out.append(raw)
            except OSError:
                continue
        return out

    def _bwrap_argv(self, layers: WriteLayers, *, network: bool) -> list[str]:
        assert self._bwrap
        argv = [self._bwrap, "--die-with-parent", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev"]
        if not network:
            # 先铺 tmpfs，再发声明的根 —— 后挂的罩住前面的，所以**声明过**的路径
            # （例如 HARNESS_JOBS_ROOT 下自己那个作业目录）会被下面的 bind 从宿主
            # 还原回来，没声明的整片消失。
            for mountpoint in self._private_socket_dirs():
                argv += ["--tmpfs", mountpoint]
        # 深度升序发射，后挂的罩住前面的 → 最具体的声明赢。旧次序（broad → readonly →
        # priority）会让住在只读根里的可写根把它内部的只读洞盖回可写（#899-A）。
        for path, mode in layers.ordered:
            argv += ["--bind" if mode == "rw" else "--ro-bind", str(path), str(path)]
        for path in layers.git:
            argv += ["--ro-bind", str(path), str(path)]
        if not network:
            argv.append("--unshare-net")
        return argv

    def prepare(self, spec: CommandSpec, *, state: Any) -> NativeLaunch:
        del state
        self._probe()
        # 私有 scratch 现在是**所有**分支的事，不再只有 Landlock：共享 /tmp 一挂就等于
        # 把别的 run 的状态目录和 /tmp/hf-jobs/*/record.json 交出去（#872）。
        scratch_dir = private_scratch()
        layers = write_layers(spec.writable_roots, spec.readonly_roots, scratch_dir=scratch_dir)
        inner: list[str] = list(spec.argv)
        unmet: list[tuple[str, str]] = []
        if not self._bwrap and self._landlock:
            # Landlock 是纯放行清单，没有 deny 层：放行共享 /tmp 就等于放行住在 /tmp
            # 下的整个 worktree（CI 上真撞过）。所以只放行自己的根 + 一个私有 scratch
            # （TMPDIR 指过去）+ 用户缓存（除非 worktree 就住在缓存目录下）+ /dev；
            # worktree 其余部分因为不在清单里而天然拒写。
            #
            # 同一个理由决定了 .git 只能靠**不接**：可写根里含 gitdir 或指针文件，就没有
            # 任何规则能再把它拒回去。这时不装守住 —— 拒绝派发并说清该传节点目录。
            # 判据 C：把下面的 `if hits` 反过来，`test_git_unwritable_is_structural` 转红。
            hits = self._git_paths_inside(layers.own)
            if hits:
                root, git_path = hits[0]
                raise IsolationContractError(
                    f"GIT_UNWRITABLE cannot be enforced: writable root {root} contains "
                    f"{git_path} (Landlock is an allow-list; there is no rule that can "
                    f"deny a path under an allowed root). Pass the node's own directory "
                    f"as the writable root, not the worktree root."
                )
            # 可写根内部声明的只读洞：Landlock 没有 deny 原语，只能靠**补集覆盖**
            # 表达（放行到洞这条链上的兄弟条目）。超出规则预算时不假装守住 ——
            # 放行整个根，并把"这条边界本次没兑现"挂在 launch 上进 transcript（#900-B1）。
            allowed, honored, why = layers.allow_list()
            roots = [str(p) for p in allowed]
            if not honored:
                unmet.append((Invariant.READONLY_CARVEOUT.value, why))
            else:
                # 洞守住了，但代价要说出来：补集覆盖放行的是**兄弟条目**，链上的
                # 目录本身不在清单里，于是"在可写根里直接新建文件"这项能力没了。
                # 不记的话，节点只会看到一个没有理由的 FileNotFoundError —— 而那
                # 正是 2026-09-15 在 CI 上花了几轮才认出来的形状。
                for chain in layers.chain_dirs():
                    unmet.append((
                        "writable_root_new_entries",
                        f"{chain}: 里面原有的条目仍可写，但**不能在它下面直接新建** —— "
                        "这个根挖了只读洞，而纯放行清单表达洞的唯一办法是只放行兄弟条目。"
                        "要在这里新建，请把可写根选在洞的外面。",
                    ))
            dev = canonical("/dev")
            if dev is not None:
                roots.append(str(dev))
            inner = [*_landlock_argv(roots, network=spec.network_access), *inner]
        else:
            # bwrap 自己 execvp 载荷失败 → `bwrap: execvp X: …` + rc 1，从咽喉看和"命令跑完
            # 返回 1"一模一样（2026-09-09 三轮就是这个长相）。Landlock 那支由启动器自己
            # 说；这一支的最内层换成 _exec.py 垫片来说，咽喉据此报 spawn_failed。
            inner = [*exec_shim_argv(), *inner]
        if self._bwrap:
            inner = [*self._bwrap_argv(layers, network=spec.network_access), "--", *inner]
        unit = None
        stop: list[str] = []
        witness = None
        if self._systemd:
            limits = spec.limits
            systemd_argv, unit, slice_unit = _systemd_run_argv(self._systemd, uuid.uuid4().hex[:12])
            memory = int(getattr(limits, "memory_bytes", 0) or 0)
            inner = [*systemd_argv, *_cgroup_properties(limits), "--", *inner]
            systemctl = shutil.which("systemctl")
            if systemctl:
                # 停 slice = 连同里面的 scope 一起停；只停 scope 会把 slice 留在用户 manager 里。
                stop = [systemctl, "--user", "stop", slice_unit]
                if memory:
                    witness = functools.partial(_memory_wall, systemctl, slice_unit, memory)
        return NativeLaunch(
            argv=inner,
            cwd=spec.cwd,
            env=payload_environment(spec.environment, scratch_dir=scratch_dir, home=spec.home),
            unit=unit,
            stop_argv=stop,
            limit_witness=witness,
            scratch_dir=scratch_dir,
            unmet=tuple(unmet),
        )
