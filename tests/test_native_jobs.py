"""原生 detached 作业：Docker Job 容器的契约（起 / 拿 runtime id / inspect / stop）由
一个脱离终端的进程组实现（core/isolation/_native_job.py + core.sandbox 兼容垫片）。

experiment 的 submit_job scheduler=local 就靠这份契约；这里按它的用法端到端真跑一遍。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import isolation, sandbox

NATIVE = isolation.native_backend_name()
_ok = NATIVE is not None and isolation.Invariant.WRITE_BOUNDARY in isolation.select_backend(NATIVE).capabilities()
pytestmark = pytest.mark.skipif(not _ok, reason="native backend cannot enforce the write boundary here")


@pytest.fixture(autouse=True)
def _jobs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setenv(isolation.EXECUTOR_ENV, NATIVE)
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_detached_job_runs_to_completion_and_is_inspectable(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    out, err = tmp_path / "job.out", tmp_path / "job.err"
    launch = sandbox.prepare_launch(
        [sys.executable, "-c", "import time; from pathlib import Path; print('started', flush=True); time.sleep(1); Path('result.txt').write_text('done\\n')"],
        cwd=work, writable_roots=[work], detached=True,
        stdout_path=out, stderr_path=err,
    )
    assert launch.container_name.startswith("hf-job-")
    started = subprocess.run(launch.argv, capture_output=True, text=True, timeout=30)
    assert started.returncode == 0, started.stderr
    runtime_id = started.stdout.strip()
    assert len(runtime_id) == 64

    seen = sandbox.inspect_container(launch.container_name)
    assert seen["exists"] and seen["managed"] and seen["id"] == runtime_id
    assert seen["kind"] == "job" and seen["name"] == launch.container_name
    assert sandbox.inspect_container(runtime_id)["name"] == launch.container_name

    assert _wait(lambda: sandbox.inspect_container(runtime_id)["status"] == "exited")
    final = sandbox.inspect_container(runtime_id)
    assert final["running"] is False and final["exit_code"] == 0
    assert (work / "result.txt").read_text() == "done\n"
    assert "started" in out.read_text()


def test_detached_job_cannot_write_outside_its_roots(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    out, err = tmp_path / "job.out", tmp_path / "job.err"
    launch = sandbox.prepare_launch(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(outside / 'leak')!r}).write_text('x')"],
        cwd=work, writable_roots=[work], readonly_roots=[tmp_path], detached=True,
        stdout_path=out, stderr_path=err,
    )
    subprocess.run(launch.argv, capture_output=True, text=True, timeout=30, check=True)
    assert _wait(lambda: sandbox.inspect_container(launch.container_name)["status"] == "exited")
    assert not (outside / "leak").exists(), "作业写出了自己的根"


def test_stop_kills_the_whole_job_group_and_removes_the_record(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    out, err = tmp_path / "job.out", tmp_path / "job.err"
    launch = sandbox.prepare_launch(
        [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)']); time.sleep(300)"],
        cwd=work, writable_roots=[work], detached=True,
        stdout_path=out, stderr_path=err,
    )
    runtime_id = subprocess.run(launch.argv, capture_output=True, text=True, timeout=30,
                                check=True).stdout.strip()
    assert _wait(lambda: sandbox.inspect_container(runtime_id)["status"] == "running")
    pid = sandbox.inspect_container(runtime_id)["pid"]

    assert sandbox.stop_container(launch.container_name, remove=True,
                                  expected_container_id=runtime_id) is True
    assert sandbox.inspect_container(runtime_id) == {"exists": False}
    from shared.lib.process_control import alive
    assert _wait(lambda: not alive(pid))


def test_stop_refuses_a_mismatched_identity(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    launch = sandbox.prepare_launch(
        [sys.executable, "-c", "import time; time.sleep(5)"], cwd=work, writable_roots=[work], detached=True,
        stdout_path=tmp_path / "o", stderr_path=tmp_path / "e",
    )
    subprocess.run(launch.argv, capture_output=True, text=True, timeout=30, check=True)
    with pytest.raises(sandbox.SandboxContractError, match="identity"):
        sandbox.stop_container(launch.container_name, expected_container_id="f" * 64)
    sandbox.stop_container(launch.container_name)


def test_foreground_compat_launch_keeps_cwd_and_the_wall(tmp_path: Path) -> None:
    """老调用方 subprocess.run(launch.argv) 不传 cwd —— 垫片把 cd 编进 argv。"""
    work = tmp_path / "work"
    work.mkdir()
    launch = sandbox.prepare_attempt_command(
        [sys.executable, "-c", "import os; from pathlib import Path; print(os.getcwd()); Path('here.txt').write_text('hi')"], state=None, cwd=work,
        writable_roots=[work], readonly_roots=[tmp_path],
    )
    done = subprocess.run(launch.argv, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(work.resolve())
    assert (work / "here.txt").exists()
    denied = sandbox.prepare_attempt_command(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(tmp_path / 'leak')!r}).write_text('x')"], state=None, cwd=work,
        writable_roots=[work], readonly_roots=[tmp_path],
    )
    assert subprocess.run(denied.argv, capture_output=True, timeout=30).returncode != 0
    assert not (tmp_path / "leak").exists()
    sys.stdout.write("")
