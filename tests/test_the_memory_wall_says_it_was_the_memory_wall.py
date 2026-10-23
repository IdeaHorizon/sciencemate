"""cgroup 把命令 OOM 杀掉，咽喉交出去的得是「撞了内存墙」，不是一个裸 -9（#1179）。

## 现场

linux 后端用 ``systemd-run --user --scope -p MemoryMax=…`` 给每条命令一片 cgroup。超了，
内核 OOM killer 发 SIGKILL —— 咽喉那边只看得到 ``rc=-9``，跟「被 SIGKILL」「被取消」长得
一样。读的一侧（``run_bash`` / ``execute_python`` / experiment ``safe_bash``）早就认
``rc=137 + HARNESS_SANDBOX_LIMIT`` → ``resource=memory``，产出方却随 Docker 一起删了。

## 为什么读 slice 不读 scope（node20 实测，systemd 255 / 内核 6.8）

scope 的 cgroup 在最后一个进程退出时由 systemd 自己删掉：``proc.wait()`` 一返回就去读
它的 ``memory.events``，8 次里 3 次文件已经没了（被 OOM 杀的 4 次里 2 次）。所以每条命令的
scope 住进它自己的 slice（``app-hf<id>.slice``）：slice 活到 cleanup 停它为止，
``memory.events`` 是层级计数，scope 里的 OOM 记在 slice 上。node20 上 5/5 读到 ``oom_kill 1``、对照 5/5 是 0。

## 这套测试钉什么

* **真走咽喉 + 真 LinuxBackend.prepare**，只把两个外部程序换成假的：``systemd-run``（认
  ``--slice=``、在一棵假 cgroup 树里给这片 slice 记下测试指定的 ``memory.events``，然后
  像真的一样 exec 载荷）和 ``systemctl``（``show`` 报 slice 的 cgroup 路径；``stop`` 像
  systemd 一样把那片 cgroup 删掉）。``stop`` 真删目录，于是「必须在 cleanup 之前读」被
  机械地钉住。
* 对照组：同一个 SIGKILL、cgroup 没记 OOM → 照旧 -9（「被 SIGKILL」和「撞内存墙」要分得开）；
  记了 OOM 但命令自己成功了 → 不改写。
* **读的一侧认得**：真走 ``run_bash`` / ``execute_python``，结果是
  ``error_code=sandbox_resource_exhausted, resource=memory``。产出方的码和读的一侧那三份
  ``{137: "memory"}`` 是同一张表，靠这一条对上。
* detached 作业没人 cleanup：监护进程在作业退出后替它停 slice，不然每个作业留一个空 slice。
* 真机（有用户级 systemd、memory 已委派、能 mlock）：真起一个超 MemoryMax 的进程，真被 OOM
  杀，咽喉报 memory。用 ``mlockall`` 是因为**有 swap 的机器上光有 MemoryMax 撞不出 OOM**
  （node20 有 160G swapfile，300 MB 压 100 MB 的上限照样 rc=0，页被换出去了）。

变异判据：咽喉里那段 ``limit_hit`` 删掉 → 前三条翻译用例 + 两条工具用例红；把它挪到
cleanup 之后（或 linux 不加 ``--slice``）→ 读不到，同样红；``_native_job`` 不跑
cleanup_argv → detached 那条红。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import isolation, sandbox
from core.bootstrap import bootstrap
from core.isolation import linux
from core.isolation.linux import LinuxBackend
from core.state import State
from shared.lib.cancellable_subprocess import spawn_and_wait

bootstrap()

_POSIX = os.name == "posix"
pytestmark = pytest.mark.skipif(not _POSIX, reason="假 systemd-run / systemctl 是 POSIX sh 脚本")

_OOM = "oom 1\noom_kill 1\noom_group_kill 0\n"
_QUIET = "oom 0\noom_kill 0\noom_group_kill 0\n"
_MEMORY = 256 * 1024**2


class _State:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.kill_event = None

    def append_transcript(self, event_type: str, **payload) -> None:
        self.events.append({"event": event_type, **payload})


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def fake_systemd(monkeypatch, tmp_path: Path):
    """真 LinuxBackend，systemd 那两个程序换成假的；cgroup 树是 tmp 下一棵假树。"""
    root = tmp_path / "cgroup"
    ctrl = tmp_path / "ctrl"
    bindir = tmp_path / "bin"
    for d in (root, ctrl, bindir):
        d.mkdir()
    slices = root / "user.slice" / "app.slice"
    _script(bindir / "systemd-run", f"""
slice=
while [ $# -gt 0 ]; do
  case "$1" in
    --slice=*) slice="${{1#--slice=}}" ;;
    --) shift; break ;;
  esac
  shift
done
mkdir -p "{slices}/$slice"
if [ -f "{ctrl}/events" ]; then cp "{ctrl}/events" "{slices}/$slice/memory.events"
else printf 'oom 0\\noom_kill 0\\n' > "{slices}/$slice/memory.events"; fi
printf 'run %s\\n' "$slice" >> "{ctrl}/log"
exec "$@"
""")
    _script(bindir / "systemctl", f"""
for last; do :; done
printf '%s\\n' "$*" >> "{ctrl}/log"
case "$2" in
  show) echo "/user.slice/app.slice/$last" ;;
  stop) rm -rf "{slices}/$last" ;;
esac
""")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(linux, "_CGROUP_ROOT", root)
    backend = LinuxBackend()
    backend._probed = True
    backend._systemd = str(bindir / "systemd-run")
    monkeypatch.setattr(isolation, "select_backend", lambda name=None: backend)
    work = tmp_path / "work"
    work.mkdir()

    def kernel_recorded(events: str) -> None:
        (ctrl / "events").write_text(events, encoding="utf-8")

    def log() -> list[str]:
        path = ctrl / "log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    return SimpleNamespace(backend=backend, slices=slices, work=work,
                           kernel_recorded=kernel_recorded, log=log)


def _run(cmd: str, work: Path):
    return asyncio.run(spawn_and_wait(
        cmd, state=_State(), timeout=60, cwd=str(work), shell=True, writable_roots=[work],
        sandbox_limits=sandbox.SandboxLimits(memory_bytes=_MEMORY)))


# ── 1. 咽喉：翻译，且只在该翻的时候翻 ─────────────────────────────────────────

def test_a_command_the_cgroup_oom_killed_comes_back_as_the_memory_wall(fake_systemd):
    fake_systemd.kernel_recorded(_OOM)
    # 载荷死前 stderr 只写了半行：标记得独占一行，不能粘在它后面。
    status, rc, _out, err = _run("printf 'half a line' >&2; kill -9 $$", fake_systemd.work)
    assert (status, rc) == ("done", isolation.SANDBOX_LIMIT_EXIT_CODES["memory"]), (status, rc, err)
    assert err == (
        f"half a line\n{isolation.SANDBOX_LIMIT_MARKER} memory oom=1 oom_kill=1 "
        f"oom_group_kill=0 memory_max={_MEMORY} returncode=-9\n".encode()), err
    # 读在 cleanup 之前：show 先于 stop；stop 之后那片 cgroup 没了（cleanup 照常收了 slice）。
    log = fake_systemd.log()
    shows = [i for i, line in enumerate(log) if " show " in line]
    stops = [i for i, line in enumerate(log) if " stop " in line]
    assert shows and stops and shows[0] < stops[0], log
    assert not any(fake_systemd.slices.iterdir()), "cleanup 没把这条命令的 slice 停掉"


def test_a_plain_sigkill_stays_a_plain_sigkill(fake_systemd):
    # 对照：同一个 SIGKILL，cgroup 没记 OOM —— 那是「被杀」，不是「撞墙」，不许冒充。
    fake_systemd.kernel_recorded(_QUIET)
    status, rc, _out, err = _run("kill -9 $$", fake_systemd.work)
    assert (status, rc) == ("done", -9), (status, rc, err)
    assert isolation.SANDBOX_LIMIT_MARKER.encode() not in err


def test_a_command_that_succeeded_is_not_rewritten(fake_systemd):
    # cgroup 记了 OOM（比如某个子进程被杀了），但命令自己成功交了结果 —— 不把成功改写成失败。
    fake_systemd.kernel_recorded(_OOM)
    status, rc, out, err = _run("echo fine", fake_systemd.work)
    assert (status, rc) == ("done", 0), (status, rc, err)
    assert out.strip() == b"fine"
    assert isolation.SANDBOX_LIMIT_MARKER.encode() not in err
    assert not any(" show " in line for line in fake_systemd.log()), "成功的命令不该多问一次 systemd"


def test_a_linux_host_without_systemd_has_no_wall_to_ask(tmp_path: Path):
    # 没有 systemd 的 linux（以及 darwin / win32）：没有墙可问，行为照旧。
    backend = LinuxBackend()
    backend._probed = True
    launch = backend.prepare(isolation.CommandSpec(
        argv=("true",), cwd=str(tmp_path), writable_roots=(tmp_path,),
        limits=sandbox.SandboxLimits()), state=None)
    try:
        assert launch.limit_hit(-9) is None
        assert not any("systemd-run" in part for part in launch.argv)
    finally:
        launch.cleanup()


# ── 2. 读的一侧认得产出方翻出来的码 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_bash_reports_the_memory_wall(fake_systemd, tmp_path: Path):
    from core.tool_registry import execute as execute_tool

    fake_systemd.kernel_recorded(_OOM)
    state = State.new(node_type="postprocess", base_dir=tmp_path / "run", project_id="p_oom1")
    res = await execute_tool("run_bash", state, cmd="kill -9 $$")
    assert res.get("error_code") == "sandbox_resource_exhausted", res
    assert res.get("resource") == "memory", res
    assert res.get("returncode") == 137 and res.get("safe_to_retry") is False, res


@pytest.mark.asyncio
async def test_execute_python_reports_the_memory_wall(fake_systemd, tmp_path: Path):
    from core.tool_registry import execute as execute_tool

    fake_systemd.kernel_recorded(_OOM)
    state = State.new(node_type="postprocess", base_dir=tmp_path / "run", project_id="p_oom2")
    res = await execute_tool(
        "execute_python", state,
        code="import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    assert res.get("error_code") == "sandbox_resource_exhausted", res
    assert res.get("resource") == "memory", res


# ── 3. detached 作业没人 cleanup：监护进程替它停 slice ─────────────────────────

def test_a_detached_job_does_not_leave_its_slice_behind(fake_systemd, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    out, err = tmp_path / "job.out", tmp_path / "job.err"
    launch = sandbox.prepare_launch(
        ["/bin/sh", "-c", "echo job-ran"], cwd=fake_systemd.work,
        writable_roots=[fake_systemd.work], detached=True, stdout_path=out, stderr_path=err)
    spec = json.loads((launch.control_dir / "launch.json").read_text(encoding="utf-8"))
    slice_unit = spec["cleanup_argv"][-1]
    assert slice_unit.startswith("app-hf") and slice_unit.endswith(".slice"), spec["cleanup_argv"]
    started = subprocess.run(launch.argv, capture_output=True, text=True, timeout=30)
    assert started.returncode == 0, started.stderr
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if any(line.endswith(f"stop {slice_unit}") for line in fake_systemd.log()):
            break
        time.sleep(0.05)
    assert any(line.endswith(f"stop {slice_unit}") for line in fake_systemd.log()), fake_systemd.log()
    assert "job-ran" in out.read_text(encoding="utf-8")
    assert not (fake_systemd.slices / slice_unit).exists()


# ── 4. 真机：真 OOM ──────────────────────────────────────────────────────────

def _real_oom_host() -> str | None:
    """不能跑的理由；None = 能跑。"""
    if not sys.platform.startswith("linux"):
        return "needs linux"
    import resource

    soft, _hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    if soft != resource.RLIM_INFINITY and soft < 512 * 1024**2:
        return f"RLIMIT_MEMLOCK={soft}: cannot pin pages past MemoryMax (swap would absorb them)"
    if not shutil.which("systemd-run"):
        return "no systemd-run"
    isolation._reset_for_tests()
    try:
        backend = isolation.select_backend("linux")
        caps = backend.capabilities()
    except Exception as exc:  # pragma: no cover - host dependent
        return f"linux backend unavailable: {exc}"
    if isolation.Invariant.MEM_CAP not in caps or isolation.Invariant.WRITE_BOUNDARY not in caps:
        return f"linux backend lacks mem_cap/write_boundary here: {backend.unavailable_reason}"
    return None


_REAL_SKIP = _real_oom_host() if _POSIX else "posix only"
#: 300 MB 钉进内存（mlockall：换不出去），压 100 MB 的 MemoryMax。
_PIN_300MB = ("import ctypes; assert ctypes.CDLL(None).mlockall(3) == 0; "
              "x = b'x' * (300 * 1024 * 1024); print(len(x))")


@pytest.mark.skipif(_REAL_SKIP is not None, reason=f"real OOM needs a systemd cgroup host: {_REAL_SKIP}")
def test_a_real_oom_kill_is_reported_as_the_memory_wall(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(isolation.EXECUTOR_ENV, "linux")
    isolation._reset_for_tests()
    try:
        limits = sandbox.SandboxLimits(memory_bytes=100 * 1024**2)
        status, rc, _out, err = asyncio.run(spawn_and_wait(
            sys.executable, "-c", _PIN_300MB, state=_State(), timeout=60, cwd=str(tmp_path),
            writable_roots=[tmp_path], sandbox_limits=limits))
        assert (status, rc) == ("done", 137), (status, rc, err)
        assert f"{isolation.SANDBOX_LIMIT_MARKER} memory ".encode() in err, err
        # 对照：同一堵墙后面一个普通的 SIGKILL 不许被说成内存墙。退出码本身看机器：
        # 咽喉直接拿到信号时是 -9；经 systemd-run --scope 包着时外层以 128+9=137 退出
        # （node20 实测）—— 两种都是"被 SIGKILL"，判据是**没有 memory 标记**，不是数字。
        status, rc, _out, err = asyncio.run(spawn_and_wait(
            "kill -9 $$", state=_State(), timeout=60, cwd=str(tmp_path), shell=True,
            writable_roots=[tmp_path], sandbox_limits=limits))
        assert status == "done" and rc in (-9, 137), (status, rc, err)
        assert isolation.SANDBOX_LIMIT_MARKER.encode() not in err
    finally:
        isolation._reset_for_tests()


@pytest.mark.skipif(_REAL_SKIP is not None, reason=f"real OOM needs a systemd cgroup host: {_REAL_SKIP}")
def test_a_real_oom_kill_needs_no_mlock_now_that_swap_is_off(tmp_path: Path, monkeypatch):
    """#1207 的直接判据：不钉页也得被杀。

    上面那条用 mlockall 是因为「有 swap 的机器上光有 MemoryMax 撞不出 OOM」—— 超限的页被换出去
    了。MemorySwapMax=0 之后，普通地占 300 MB 压 100 MB 的上限，在有 swap 的机器（node20：
    160 GB swapfile）上也必须当场 OOM 并被报成内存墙，而不是换页"慢慢跑完"。
    """
    monkeypatch.setenv(isolation.EXECUTOR_ENV, "linux")
    isolation._reset_for_tests()
    try:
        limits = sandbox.SandboxLimits(memory_bytes=100 * 1024**2)
        status, rc, _out, err = asyncio.run(spawn_and_wait(
            sys.executable, "-c", "x = bytearray(300 * 1024 * 1024); print(len(x))",
            state=_State(), timeout=60, cwd=str(tmp_path),
            writable_roots=[tmp_path], sandbox_limits=limits))
        assert (status, rc) == ("done", 137), (
            f"没钉页的 300 MB 在 100 MB 的墙后面活下来了 —— swap 又成了后门: {(status, rc, err)}")
        assert f"{isolation.SANDBOX_LIMIT_MARKER} memory ".encode() in err, err
    finally:
        isolation._reset_for_tests()
