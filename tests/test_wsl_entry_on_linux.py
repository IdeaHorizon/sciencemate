"""WSL 执行目标在发行版里那一层（``core/isolation/_wsl_entry.py``），在任何 Linux 上真跑。

它不关心自己是不是在 WSL 里：读 spec、用 linux 后端 prepare、起载荷、收尾。所以 CI 的
Linux 与 9800x3d 的 WSL 发行版跑的是同一组断言。

两种机器两种期待：

* 没有 bubblewrap → ``probe`` 说守不住写边界、``run`` 拒绝派发（启动器标记 + 126），**载荷
  一行没跑**。WSL 上这是对的：interop 能把命令送出 Landlock 那堵墙（11-03 真机实测）。
* 有 bubblewrap → 真跑、越界写被拒、``kill`` 停得下、Windows 那头（这里用管道模拟）断了
  整组收掉。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux side only")

ENTRY = Path(__file__).resolve().parents[1] / "core" / "isolation" / "_wsl_entry.py"
MARKER = "HARNESS_ISOLATION_ERROR"


def _entry(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-B", str(ENTRY), *args], capture_output=True,
                          text=True, timeout=120, **kw)


@pytest.fixture(scope="module")
def report() -> dict:
    out = _entry("probe")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _spec(tmp_path: Path, argv: list[str], **extra) -> tuple[Path, str]:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    ident = uuid.uuid4().hex[:16]
    spec = {"ident": ident, "argv": argv, "cwd": str(work), "writable": [str(work)],
            "readonly": [], "limits": {"memory_bytes": 0, "pids": 0, "cpus": 0,
                                       "walltime_seconds": 60},
            "env": {}, "network": False, "home": "host", **extra}
    path = tmp_path / f"spec-{ident}.json"
    path.write_text(json.dumps(spec))
    return path, ident


def test_probe_says_what_the_wall_is(report) -> None:
    for key in ("caps", "write_boundary", "net_scope", "reason", "kernel", "python"):
        assert key in report
    if not report["write_boundary"]:
        assert "bubblewrap" in report["reason"] or report["reason"]


def test_run_refuses_or_runs_inside_the_wall(report, tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    spec, _ = _spec(tmp_path, ["/bin/sh", "-c", f"echo in > in.txt; echo x > {outside}; echo done"])
    out = _entry("run", str(spec))
    assert not spec.exists(), "spec 读完即删"
    if not report["write_boundary"]:
        assert out.returncode == 126 and MARKER in out.stderr
        assert not (tmp_path / "work" / "in.txt").exists(), "拒绝派发时载荷一行都不能跑"
        return
    assert (tmp_path / "work" / "in.txt").read_text().strip() == "in"
    assert not outside.exists(), "可写根之外写进去了"
    assert "done" in out.stdout


def _wait_for(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


def test_kill_stops_the_command(report, tmp_path) -> None:
    if not report["write_boundary"]:
        pytest.skip("no wall on this host (needs bubblewrap)")
    spec, ident = _spec(tmp_path, ["/bin/sh", "-c", "touch started; sleep 60"])
    proc = subprocess.Popen([sys.executable, "-I", "-B", str(ENTRY), "run", str(spec)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_for(tmp_path / "work" / "started")
        t0 = time.monotonic()
        assert _entry("kill", ident).returncode == 0
        proc.wait(timeout=15)
        assert time.monotonic() - t0 < 10
    finally:
        proc.kill()


def test_the_command_dies_when_the_windows_side_goes_away(report, tmp_path) -> None:
    """worker 崩了没人调 kill：wsl.exe 一死，本进程 stdout 那头断开 → 整组收掉。"""
    if not report["write_boundary"]:
        pytest.skip("no wall on this host (needs bubblewrap)")
    marker = f"sleep 60.{int(uuid.uuid4().int % 10**9):09d}"   # 一条认得出来的 sleep
    spec, _ = _spec(tmp_path, ["/bin/sh", "-c", f"touch started; {marker}"])
    read_end, write_end = os.pipe()
    proc = subprocess.Popen([sys.executable, "-I", "-B", str(ENTRY), "run", str(spec)],
                            stdout=write_end, stderr=write_end)
    os.close(write_end)
    try:
        _wait_for(tmp_path / "work" / "started")
        os.close(read_end)                     # Windows 那一头没了
        proc.wait(timeout=15)
        survivors = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        assert survivors.stdout.strip() == "", "载荷在父进程消失后还活着"
    finally:
        proc.kill()
