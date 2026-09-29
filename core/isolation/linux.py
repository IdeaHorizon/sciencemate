"""linux 后端：Landlock / bwrap 守写边界，bwrap 断网，systemd 的 cgroup 守资源。

三样东西各自探测、各自记账，谁在就用谁：

* **bwrap**（需要 user namespace）：``--ro-bind / /`` 全盘只读 + 按层挂回可写；
  ``--ro-bind`` 压住 ``.git``；``--unshare-net`` 断网。有它就同时有
  WRITE_BOUNDARY / GIT_UNWRITABLE / NET_DENY。
* **Landlock**（内核 ≥ 5.13，不需要 userns）：bwrap 起不来时的写墙主力（CI runner、
  node20、HPC 作业里 userns 常被禁 —— node20 就是 ``apparmor_restrict_unprivileged_userns=1``）。
  纯放行清单：``.git`` 不靠它挡，靠**结构判据**（可写根不含 gitdir / 指针文件，含了就在
  ``prepare`` 拒绝派发，见 ``_native.git_paths_inside_writable``）→ GIT_UNWRITABLE。
  **ABI ≥ 4（内核 ≥ 6.7）管 TCP**：探针证明 connect 被拒才声明 NET_DENY，记账
  ``net_scope: tcp``（只管 TCP bind/connect；UDP / ICMP 不在内，bwrap 才是 ``all``）。
* **systemd-run --user --scope**：MemoryMax / TasksMax / CPUQuota，也就是 cgroup v2。
  探针成功才声明 MEM_CAP / PIDS_CAP；整个 scope 在 cleanup 时 ``systemctl --user stop``，
  比 PID 快照差集更准（``setsid`` 逃逸也逃不出 cgroup）。

层次（外→内）：systemd-run → bwrap → landlock 启动器 → 命令。有 bwrap 时不走 landlock
启动器，最内层换成 ``_exec.py`` 垫片 —— 最内层必须是我们的代码，载荷起不起得来才有人说
（``core.isolation.LAUNCHER_ERROR_MARKER``）。
"""

from __future__ import annotations

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
_PROBE_CONNECT = (
    "import errno, socket, sys\n"
    "s = socket.socket(); s.settimeout(3)\n"
    "try:\n    s.connect(('127.0.0.1', int(sys.argv[1])))\n"
    "except PermissionError:\n    sys.exit(0)\n"
    "except OSError as e:\n"
    "    sys.exit(0 if e.errno in (errno.EACCES, errno.EPERM) else 3)\n"
    "sys.exit(4)\n"
)


def _landlock_argv(roots: list[str], *, network: bool) -> list[str]:
    return [sys.executable, "-I", "-B", str(_LANDLOCK_EXEC),
            "--network", "allow" if network else "deny", json.dumps(roots), "--"]


def _probe_landlock() -> tuple[int, bool, str]:
    """→ (abi, 网络是否真拒, 原因)。abi=0 = 写墙不可用；网络那一位只在 abi ≥ 4 时才可能 True。

    网络探针带**对照**：同一个监听端口，``--network allow`` 必须连得上、``deny`` 必须被拒。
    只测 deny 的话，监听器没起来 / 端口错了也会"被拒"，那是假阳性
    （[[feedback_baseline_can_vanish]]）。
    """
    if not _LANDLOCK_EXEC.exists():
        return 0, False, "landlock launcher missing"
    import socket
    import subprocess

    try:
        out = subprocess.run([sys.executable, "-I", "-B", str(_LANDLOCK_EXEC), "abi"],
                             capture_output=True, text=True, timeout=10, check=False)
        abi = int((out.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0, False, "landlock abi probe failed"
    if abi < 1:
        return 0, False, "kernel has no Landlock (need >= 5.13, or seccomp blocks it)"
    with tempfile.TemporaryDirectory(prefix="hf-landlock-probe-") as tmp:
        allowed = Path(tmp).resolve() / "allowed"
        allowed.mkdir()
        outside = Path(tmp).resolve() / "outside"
        roots = [str(allowed), "/dev"]
        if run_ok([*_landlock_argv(roots, network=True), "/bin/sh", "-c",
                   f"echo x > '{outside}'"]) or outside.exists():
            return 0, False, "landlock did not deny a write outside the allowed root"
        if not run_ok([*_landlock_argv(roots, network=True), "/bin/sh", "-c",
                       f"echo x > '{allowed / 'ok'}'"]):
            return 0, False, "landlock denied an allowed write"
        if abi < 4:
            return abi, False, ""
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = str(listener.getsockname()[1])
            probe = [sys.executable, "-I", "-B", "-c", _PROBE_CONNECT, port]
            control = subprocess.run([*_landlock_argv(roots, network=True), *probe],
                                     capture_output=True, timeout=15, check=False)
            if control.returncode != 4:
                return abi, False, (
                    "landlock network probe has no control (allow could not connect)"
                )
            denied = subprocess.run([*_landlock_argv(roots, network=False), *probe],
                                    capture_output=True, timeout=15, check=False)
            if denied.returncode != 0:
                return abi, False, "landlock abi>=4 present but did not deny a TCP connect"
        except (OSError, subprocess.TimeoutExpired):
            return abi, False, "landlock network probe failed"
        finally:
            listener.close()
    return abi, True, ""


def _probe_systemd() -> tuple[str | None, str]:
    exe = shutil.which("systemd-run")
    if not exe:
        return None, "systemd-run not installed"
    unit = f"hf-probe-{uuid.uuid4().hex[:8]}.scope"
    if not run_ok([exe, "--user", "--scope", "--quiet", f"--unit={unit}",
                   "-p", "MemoryMax=64M", "-p", "TasksMax=16", "-p", "CPUQuota=50%",
                   "/bin/true"]):
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
        self._landlock_abi, self._landlock_net, why = _probe_landlock()
        self._landlock = self._landlock_abi >= 1
        if why:
            self._reasons["landlock"] = why
        elif self._landlock and not self._landlock_net:
            self._reasons["landlock"] = (
                f"landlock abi {self._landlock_abi} has no network rules "
                "(need >= 4, kernel >= 6.7)"
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
            # 探针跑的正是 `-p MemoryMax=64M -p TasksMax=16 -p CPUQuota=50%` —— 它成功
            # 就意味着这三条 cgroup 限额这台机器都吃得下。CPU 此前没有名字，于是
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

        ``all`` = bwrap 整个网络命名空间；``tcp`` = Landlock ABI 4 只管 TCP bind/connect，
        UDP / ICMP / unix socket 不在内；``none`` = 没守到。UI 原样带出，不许圆。
        """
        self._probe()
        if self._bwrap:
            return "all"
        if self._landlock_net:
            return "tcp"
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
        if self._systemd:
            limits = spec.limits
            unit = f"hf-{uuid.uuid4().hex[:12]}.scope"
            props = []
            memory = int(getattr(limits, "memory_bytes", 0) or 0)
            pids = int(getattr(limits, "pids", 0) or 0)
            cpus = float(getattr(limits, "cpus", 0) or 0)
            if memory:
                props += ["-p", f"MemoryMax={memory}"]
            if pids:
                props += ["-p", f"TasksMax={pids}"]
            if cpus:
                props += ["-p", f"CPUQuota={int(cpus * 100)}%"]
            inner = [self._systemd, "--user", "--scope", "--quiet", f"--unit={unit}", *props,
                     "--", *inner]
            systemctl = shutil.which("systemctl")
            if systemctl:
                stop = [systemctl, "--user", "stop", unit]
        return NativeLaunch(
            argv=inner,
            cwd=spec.cwd,
            env=payload_environment(spec.environment, scratch_dir=scratch_dir, home=spec.home),
            unit=unit,
            stop_argv=stop,
            scratch_dir=scratch_dir,
            unmet=tuple(unmet),
        )
