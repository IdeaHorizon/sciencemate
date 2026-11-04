"""WSL 执行目标的 Linux 一侧：在发行版里用 linux 后端把命令关进墙、看着它跑完、收尾。

由 Windows 一侧（``core.isolation.wsl``）经 ``wsl.exe -d <发行版> --exec python3 -I -B`` 起，
跑的是发行版自己的 ``python3``，所以**只用标准库**：仓库在 ``/mnt/<盘>/…`` 下可读，
``core.isolation`` 整个包只依赖标准库（``core.secrets`` / ``shared.lib.platform_env`` 同），
但 ``core/__init__.py`` 会 import 整套 loop hooks —— 这里用一个空壳 ``core`` 包绕开它。

三个子命令：

* ``probe``：探这台发行版能守到哪几条不变量，打一行 JSON。
* ``run <spec 文件>``：读 Windows 一侧写好的 spec（读完即删），用 linux 后端 ``prepare``，
  起载荷、等它退出、收尾，退出码原样交回（撞了资源墙就按 ``SANDBOX_LIMIT`` 词表翻）。
* ``kill <ident>``：按 ``run`` 记下的进程组结束那条命令（咽喉取消 / 超时时由 Windows 一侧调）。

## 为什么要求 bubblewrap（11-03 9800x3d 真机实测）

WSL 的 interop 让发行版里**任何**进程直接执行 ``cmd.exe`` / ``powershell.exe`` 等 Windows
程序，Windows 一侧以用户的身份（Medium IL）把它起起来。实测从 ``_landlock_exec.py --network
deny`` 里面照样起得来（``WSL_INTEROP`` 去掉也一样，会回落到 ``/run/WSL/1_interop``）——
Landlock 不管 unix socket 的 ``connect``，seccomp 那层只禁 AF_INET。于是只有 Landlock 时，写边界
与断网都是假的。bubblewrap 把 ``/run/WSL`` 换成空 tmpfs 之后 interop 起不来（同机实测），所以
没有 bubblewrap = 守不住写边界 = 按 I1 拒绝，并说清怎么装。

## 父进程没了怎么办

咽喉取消 / 超时时会调 ``kill``；但 worker 自己崩了时没人调。``wsl.exe`` 一死，本进程的
stdout / stderr 那头就断了 —— 每 0.2 秒 poll 一次这两个 fd，看到 ``POLLHUP`` / ``POLLERR``
就把载荷整组杀掉。
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = "HARNESS_ISOLATION_ERROR"  # 与 core.isolation.LAUNCHER_ERROR_MARKER 同一个字面量
LAUNCH_ENV = "HF_WSL_LAUNCH"
#: 不论命令是否联网都藏起来的目录：WSL interop 的 socket 在这里。
ALWAYS_HIDDEN = ("/run/WSL",)
BWRAP_INSTALL = "sudo apt install bubblewrap   # 或把 bwrap 放进 ~/.local/bin"


def _state_dir() -> Path:
    path = Path(f"/tmp/hf-wsl-{os.getuid()}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _bootstrap() -> None:
    """让 ``core.isolation`` 可 import，又不执行 ``core/__init__.py``。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if "core" not in sys.modules:
        core = types.ModuleType("core")
        core.__path__ = [str(ROOT / "core")]  # type: ignore[attr-defined]
        sys.modules["core"] = core
    local_bin = Path.home() / ".local" / "bin"
    if (local_bin / "bwrap").exists():
        os.environ["PATH"] = f"{local_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    # ``wsl.exe --exec`` 起的会话拿不到用户总线，``systemd-run --user`` 就起不来；
    # 用户 manager 在（linger / 登录过）时把总线地址补上，内存墙才有机会。
    runtime = Path(f"/run/user/{os.getuid()}")
    if (runtime / "bus").exists():
        os.environ.setdefault("XDG_RUNTIME_DIR", str(runtime))
        os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime / 'bus'}")


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _backend():
    from core.isolation.linux import LinuxBackend

    cache = _state_dir() / "linux-facts.json"
    key = {"boot_id": _boot_id(), "kernel": os.uname().release, "python": sys.version,
           "path": os.environ.get("PATH", "")}
    try:
        cached = json.loads(cache.read_text())
        if cached.get("key") == key:
            return LinuxBackend.from_facts(cached["facts"], always_hidden=ALWAYS_HIDDEN)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    backend = LinuxBackend(always_hidden=ALWAYS_HIDDEN)
    facts = backend.facts()
    try:
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key, "facts": facts}))
        os.replace(tmp, cache)
    except OSError:
        pass
    return backend


def _report(backend) -> dict:
    from core.isolation import Invariant

    caps = set(backend.capabilities())
    reason = backend.unavailable_reason
    if not backend.facts().get("bwrap"):
        # 没有 bubblewrap：interop 能把命令送出墙（见模块说明）。写边界与断网都不成立。
        caps = set()
        reason = ("bubblewrap is missing in this distro; without it WSL interop lets a command "
                  f"start Windows programs outside the wall. Fix: {BWRAP_INSTALL}"
                  + (f" ({reason})" if reason else ""))
    os_release = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            key, _, value = line.partition("=")
            os_release[key] = value.strip().strip('"')
    except OSError:
        pass
    return {
        "caps": sorted(c.value for c in caps),
        "write_boundary": Invariant.WRITE_BOUNDARY in caps,
        "net_scope": backend.net_scope if caps else "none",
        "job_loopback": backend.job_loopback if caps else "host_shared",
        "write_boundary_scope": backend.write_boundary_scope if caps else "none",
        "unix_socket_reachable": backend.unix_socket_reachable,
        "pids_event_source": backend.pids_event_source,
        "reason": reason,
        "distro": os_release.get("PRETTY_NAME", ""),
        "kernel": os.uname().release,
        "python": sys.version.split()[0],
        "user": os.environ.get("USER") or str(os.getuid()),
        "home": str(Path.home()),
    }


def _refuse(kind: str, detail: str) -> int:
    sys.stderr.write(f"{MARKER} {kind}:{detail}\n")
    sys.stderr.flush()
    return 126


def _payload_env(env: dict) -> dict:
    """墙里的环境：去掉 interop 的指路变量与 Windows 那半截 PATH（``/mnt/<盘>/…``）。"""
    out = {k: v for k, v in env.items() if k not in ("WSL_INTEROP", "WSLENV")}
    path = [p for p in out.get("PATH", "").split(":") if p and not p.startswith("/mnt/")]
    out["PATH"] = ":".join(path) or "/usr/local/bin:/usr/bin:/bin"
    return out


def _sweep(ident: str, keep: int) -> list[int]:
    """杀掉环境里带着本次标记、却已经逃出进程组的后代（setsid / nohup）。"""
    needle = f"{LAUNCH_ENV}={ident}".encode()
    killed: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in (keep, os.getpid()):
            continue
        try:
            if needle in (entry / "environ").read_bytes().split(b"\0"):
                os.kill(int(entry.name), signal.SIGKILL)
                killed.append(int(entry.name))
        except (OSError, ValueError):
            continue
    return killed


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run(spec_path: str) -> int:
    from types import SimpleNamespace

    from core.isolation import (
        SANDBOX_LIMIT_EXIT_CODES,
        SANDBOX_LIMIT_MARKER,
        CommandSpec,
        IsolationContractError,
    )

    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _refuse("spec_unreadable", f"{spec_path}: {exc}")
    finally:
        try:
            os.remove(spec_path)
        except OSError:
            pass
    backend = _backend()
    report = _report(backend)
    if not report["write_boundary"]:
        return _refuse("wall_unavailable", report["reason"] or "write boundary not enforceable")
    limits = SimpleNamespace(**(spec.get("limits") or {}))
    try:
        command = CommandSpec(
            argv=tuple(spec["argv"]),
            cwd=spec["cwd"],
            writable_roots=tuple(Path(p) for p in spec["writable"]),
            readonly_roots=tuple(Path(p) for p in spec.get("readonly") or ()),
            limits=limits,
            environment=spec.get("env") or None,
            network_access=bool(spec.get("network")),
            home=spec.get("home") or "host",
            capabilities=frozenset(spec.get("capabilities") or ()),
            ephemeral=bool(spec.get("ephemeral")),
        )
        launch = backend.prepare(command, state=None)
    except (IsolationContractError, KeyError, OSError) as exc:
        return _refuse("contract", str(exc))
    ident = str(spec["ident"])
    env = _payload_env(dict(launch.env or os.environ))
    env[LAUNCH_ENV] = ident
    try:
        proc = subprocess.Popen(launch.argv, cwd=launch.cwd, env=env, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as exc:
        launch.cleanup()
        return _refuse("spawn", f"{launch.argv[0]}: {exc}")
    pidfile = _state_dir() / f"{ident}.pid"
    pidfile.write_text(str(proc.pid))

    def _on_signal(signum, _frame):
        _kill_group(proc.pid, signal.SIGKILL)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _on_signal)
    watch = select.poll()
    for fd in (1, 2):
        watch.register(fd, select.POLLERR | select.POLLHUP)
    orphaned = False
    while proc.poll() is None:
        try:
            events = watch.poll(200)
        except InterruptedError:
            continue
        for fd, mask in events:
            if mask & select.POLLNVAL:      # fd 本来就是关着的：不能拿它判父进程，别空转
                watch.unregister(fd)
        if any(mask & (select.POLLERR | select.POLLHUP) for _fd, mask in events):
            orphaned = True                  # wsl.exe 那头没了：没人等结果，整组收掉
            _kill_group(proc.pid, signal.SIGKILL)
            proc.wait()
            break
    returncode = proc.returncode
    hit = None
    try:
        hit = launch.limit_hit(returncode)
    except Exception:  # noqa: BLE001 - 问不出来就照旧交原始退出码
        hit = None
    _kill_group(proc.pid, signal.SIGKILL)   # GROUP_KILL：留下的后台进程不能变成下一条命令的状态
    _sweep(ident, keep=proc.pid)
    launch.cleanup()
    try:
        pidfile.unlink()
    except OSError:
        pass
    if orphaned:
        return 125
    if hit is not None:
        resource, detail = hit
        sys.stderr.write(f"\n{SANDBOX_LIMIT_MARKER} {resource} {detail} returncode={returncode}\n")
        sys.stderr.flush()
        return SANDBOX_LIMIT_EXIT_CODES[resource]
    return returncode if returncode >= 0 else 128 - returncode


def kill(ident: str) -> int:
    pidfile = _state_dir() / f"{ident}.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return 0                              # 已经结束了 —— 目的已达成
    _kill_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return 0
        time.sleep(0.1)
    _kill_group(pid, signal.SIGKILL)
    return 0


def main(argv: list[str]) -> int:
    if sys.platform == "win32":
        # 这一层只属于发行版：进程组 / SIGKILL 在 Windows 上会静默失效（见
        # tests/test_process_control_is_the_only_process_control.py 的登记）。
        sys.stderr.write(
            f"{MARKER} wrong_side:_wsl_entry.py runs inside a WSL distro, not on Windows\n")
        return 126
    if not argv or argv[0] not in ("probe", "run", "kill"):
        sys.stderr.write("usage: _wsl_entry.py probe | run <spec.json> | kill <ident>\n")
        return 64
    _bootstrap()
    if argv[0] == "probe":
        print(json.dumps(_report(_backend())))
        return 0
    if len(argv) != 2:
        sys.stderr.write(f"usage: _wsl_entry.py {argv[0]} <arg>\n")
        return 64
    return run(argv[1]) if argv[0] == "run" else kill(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
