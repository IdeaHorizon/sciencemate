"""wsl 后端：Windows 主机上的执行目标 ``wsl:<发行版>``（RFC_WINDOWS_EXECUTION_TARGETS D1 / D9）。

worker 跑在 Medium IL，从这里经 ``wsl.exe -d <发行版> --exec`` 进发行版；墙在对面：
发行版里的 ``_wsl_entry.py`` 用 linux 后端（bubblewrap + Landlock + seccomp，有用户总线时
再加 systemd cgroup）关住命令。这里只做三件事：

1. **探**：发行版能守到哪几条不变量，由 ``_wsl_entry.py probe`` 在发行版里真探一次答。
2. **翻**：Windows 路径 → 发行版路径（``C:\\x`` → ``/mnt/c/x``，挂载根按 ``wslpath`` 实测，
   不假设 ``/mnt``）；argv 里的受管 shell / 解释器换成发行版自己的（``bash`` → ``/bin/bash``，
   平台的 Windows Python → 发行版的 ``python3``）。命令正文（``bash -c`` 后面那段）一个字不动。
3. **交**：spec 写进一个临时文件交给对面（不走命令行 —— ``wsl.exe`` 与 Windows 命令行
   都有长度和引号的坑），咽喉起 ``wsl.exe``；取消 / 超时由 ``terminate`` 调对面的 ``kill``。

为什么不在 Windows 一侧做墙：Low IL / AppContainer 都够不着 WSL 服务（#1263，11-03 真机），
而且墙应该在命令真正跑的那个内核里。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from . import CommandSpec, Invariant, IsolationContractError
from ._native import NativeLaunch

_ENTRY = Path(__file__).with_name("_wsl_entry.py")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC = re.compile(r"^\\\\(?:wsl\$|wsl\.localhost)\\([^\\]+)(\\.*)?$", re.IGNORECASE)


def wsl_exe() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return str(Path(root) / "System32" / "wsl.exe")


def _decode(raw: bytes) -> str:
    """``wsl.exe`` 自己的话是 UTF-16LE，发行版里程序的输出是 UTF-8。"""
    if len(raw) >= 2 and raw[1:2] == b"\x00":
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def list_distros(timeout: float = 30.0) -> list[str]:
    """本机注册了哪些 WSL 发行版（``wsl.exe -l -q``）。不是 Windows / 没装 WSL → 空。"""
    if sys.platform != "win32" or not Path(wsl_exe()).exists():
        return []
    try:
        out = subprocess.run([wsl_exe(), "-l", "-q"], capture_output=True, timeout=timeout,
                             check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    names = [line.strip().strip("\x00") for line in _decode(out.stdout).splitlines()]
    return [name for name in names if name]


@dataclass
class WslLaunch(NativeLaunch):
    """收尾只删本机临时目录；``stop_argv``（对面的 ``kill``）只在取消 / 超时时跑。"""

    def cleanup(self) -> None:
        if self.scratch_dir is not None:
            shutil.rmtree(self.scratch_dir, ignore_errors=True)


class WslBackend:
    name = "wsl"

    def __init__(self, distro: str) -> None:
        self.distro = distro
        self._probed = False
        self._report: dict[str, Any] = {}
        self._mount_prefix: str | None = None
        self._entry_linux: str | None = None
        self._reason = ""

    @property
    def target_spec(self) -> str:
        return f"wsl:{self.distro}"

    def _exec(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run([wsl_exe(), "-d", self.distro, "--cd", "/", "--exec", *args],
                              capture_output=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL)

    def _probe(self) -> None:
        if self._probed:
            return
        self._probed = True
        if sys.platform != "win32":
            self._reason = f"the wsl target needs a Windows host (this is {sys.platform})"
            return
        if not Path(wsl_exe()).exists():
            self._reason = f"{wsl_exe()} not found: WSL is not installed"
            return
        try:
            # 第一次可能要把 WSL 虚拟机拉起来，给足时间。
            out = self._exec(["wslpath", "-u", "C:\\"], timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._reason = f"wsl.exe did not answer: {exc}"
            return
        text = _decode(out.stdout).strip()
        if out.returncode != 0 or not text.startswith("/") or not text.rstrip("/").endswith("/c"):
            said = _decode(out.stderr).strip() or _decode(out.stdout).strip() or out.returncode
            self._reason = f"distro {self.distro!r} is not usable ({said})"
            return
        self._mount_prefix = text.rstrip("/")[: -len("c")]      # "/mnt/c/" → "/mnt/"
        self._entry_linux = self.to_linux(_ENTRY)
        try:
            out = self._exec(["python3", "-I", "-B", self._entry_linux, "probe"], timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._reason = f"probe inside {self.distro} did not finish: {exc}"
            return
        try:
            self._report = json.loads(_decode(out.stdout).strip().splitlines()[-1])
        except (ValueError, IndexError):
            self._reason = (f"probe inside {self.distro} failed (rc={out.returncode}): "
                            f"{_decode(out.stderr).strip()[-400:]}")
            return
        self._reason = str(self._report.get("reason") or "")

    # ── 路径 ──────────────────────────────────────────────────────────────────

    def to_linux(self, path: Path | str) -> str:
        """Windows 绝对路径 → 发行版里的路径。翻不了就拒绝，不猜。"""
        self._probe_prefix_only()
        raw = str(path)
        unc = _UNC.match(raw)
        if unc:
            if unc.group(1).lower() != self.distro.lower():
                raise IsolationContractError(
                    f"{raw} belongs to WSL distro {unc.group(1)!r}, not {self.distro!r}")
            return (unc.group(2) or "\\").replace("\\", "/")
        if not _DRIVE.match(raw):
            raise IsolationContractError(
                f"cannot map {raw!r} into WSL: not an absolute Windows path")
        win = PureWindowsPath(raw)
        drive = win.drive[0].lower()
        rest = "/".join(win.parts[1:])
        assert self._mount_prefix is not None
        return f"{self._mount_prefix}{drive}" + (f"/{rest}" if rest else "")

    def to_windows(self, path: str) -> str | None:
        """发行版里的路径 → Windows 路径；不在 Windows 盘上（``/home``、``/tmp``）→ None。

        给按 Windows 路径判写入目标的人用（experiment 的语义闸今天按 MSYS 规则解析绝对路径，
        WSL 目标下 ``/mnt/c/…`` 会被判到错的地方 —— 判错的方向是拦，不是放）。
        """
        self._probe_prefix_only()
        assert self._mount_prefix is not None
        prefix = self._mount_prefix
        if not path.startswith(prefix) or len(path) < len(prefix) + 1:
            return None
        rest = path[len(prefix):]
        drive, _, tail = rest.partition("/")
        if len(drive) != 1 or not drive.isalpha():
            return None
        return f"{drive.upper()}:\\" + tail.replace("/", "\\")

    def _probe_prefix_only(self) -> None:
        if self._mount_prefix is None:
            self._probe()
        if self._mount_prefix is None:
            raise IsolationContractError(self._reason or f"WSL distro {self.distro!r} unavailable")

    def _linux_argv(self, argv: tuple[str, ...]) -> list[str]:
        from shared.lib.shell import bash_shell, posix_shell

        def same(a: str, b: str) -> bool:
            return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

        out: list[str] = []
        first, rest = argv[0], argv[1:]
        try:
            managed_bash = bash_shell()
        except FileNotFoundError:
            managed_bash = ""
        if managed_bash and _DRIVE.match(first) and same(first, managed_bash):
            out.append("/bin/bash")
        elif _DRIVE.match(first) and same(first, posix_shell()):
            out.append("/bin/sh")
        elif sys.executable and _DRIVE.match(first) and same(first, sys.executable):
            out.append("python3")                 # 发行版自己的解释器，不是平台的 Windows Python
        elif _DRIVE.match(first) or _UNC.match(first):
            out.append(self.to_linux(first))
        else:
            out.append(first)
        for arg in rest:
            # 整个参数就是一个 Windows 绝对路径时才翻（脚本路径之类）；命令正文不动。
            if ("\n" not in arg) and (_DRIVE.match(arg) or _UNC.match(arg)):
                out.append(self.to_linux(arg))
            else:
                out.append(arg)
        return out

    # ── 能力 ──────────────────────────────────────────────────────────────────

    def capabilities(self) -> frozenset[Invariant]:
        self._probe()
        caps = set()
        for name in self._report.get("caps") or ():
            try:
                caps.add(Invariant(name))
            except ValueError:
                continue
        return frozenset(caps) if Invariant.WRITE_BOUNDARY in caps else frozenset()

    @property
    def unavailable_reason(self) -> str:
        self._probe()
        return self._reason

    @property
    def net_scope(self) -> str:
        self._probe()
        return str(self._report.get("net_scope") or "none")

    @property
    def job_loopback(self) -> str:
        """发行版那一侧 LinuxBackend 报的（wsl 目标要求 bwrap，所以通常是 private_netns）。"""
        self._probe()
        return str(self._report.get("job_loopback") or "host_shared")

    @property
    def write_boundary_scope(self) -> str:
        self._probe()
        return str(self._report.get("write_boundary_scope") or "none")

    @property
    def unix_socket_reachable(self) -> bool | str:
        self._probe()
        value = self._report.get("unix_socket_reachable", True)
        return value if isinstance(value, (bool, str)) else True

    @property
    def pids_event_source(self) -> str | None:
        self._probe()
        value = self._report.get("pids_event_source")
        return value if isinstance(value, str) else None

    def describe(self) -> dict[str, Any]:
        """给开局说明用的事实（发行版、内核、python、用户、挂载根）。"""
        self._probe()
        return {**self._report, "distro_name": self.distro, "mount_prefix": self._mount_prefix,
                "reason": self._reason}

    # ── 起命令 ────────────────────────────────────────────────────────────────

    def prepare(self, spec: CommandSpec, *, state: Any) -> WslLaunch:
        del state
        self._probe()
        if Invariant.WRITE_BOUNDARY not in self.capabilities():
            raise IsolationContractError(
                f"the wsl:{self.distro} target cannot enforce the write boundary (I1): "
                f"{self._reason or 'probe gave no answer'}")
        assert self._entry_linux is not None
        limits = spec.limits
        ident = uuid.uuid4().hex[:16]
        payload = {
            "ident": ident,
            "argv": self._linux_argv(spec.argv),
            "cwd": self.to_linux(spec.cwd),
            "writable": [self.to_linux(p) for p in spec.writable_roots],
            "readonly": [self.to_linux(p) for p in spec.readonly_roots],
            "limits": {
                "memory_bytes": int(getattr(limits, "memory_bytes", 0) or 0),
                "pids": int(getattr(limits, "pids", 0) or 0),
                "cpus": float(getattr(limits, "cpus", 0) or 0),
                "walltime_seconds": int(getattr(limits, "walltime_seconds", 0) or 0),
            },
            "env": {str(k): (self.to_linux(v) if _DRIVE.match(str(v)) and ";" not in str(v)
                             else str(v))
                    for k, v in (spec.environment or {}).items()},
            "network": bool(spec.network_access),
            "home": spec.home,
            "capabilities": sorted(spec.capabilities),
            "ephemeral": bool(spec.ephemeral),
        }
        handoff = Path(tempfile.mkdtemp(prefix="hf-wsl-"))
        spec_file = handoff / "spec.json"
        spec_file.write_text(json.dumps(payload), encoding="utf-8")
        entry = ["python3", "-I", "-B", self._entry_linux]
        base = [wsl_exe(), "-d", self.distro, "--cd", "/", "--exec", *entry]
        from core.secrets import is_secret_name

        return WslLaunch(
            argv=[*base, "run", self.to_linux(spec_file)],
            cwd=spec.cwd,
            env={k: v for k, v in os.environ.items() if not is_secret_name(k)},
            stop_argv=[*base, "kill", ident],
            scratch_dir=handoff,
            cwd_in_argv=True,
        )


__all__ = ["WslBackend", "WslLaunch", "list_distros", "wsl_exe"]
