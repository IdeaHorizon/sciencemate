"""受管作业里的本机多进程通信：能做的地方做得成、守得住；做不到的地方提交前说清（#1166）。

lujy 09-28（node20，FVCOM）：MPI 版编译成功，执行时 `MPI_Init` 报 `bind(): Permission denied` ——
当时那台机器走的是纯 Landlock，作业里的回环 TCP 和外网一起断了。

10-21 裁决要的范围：作业内回环 TCP 可用，外网与无关宿主服务仍断。2026-11-04 node20 实测：

| 后端 | 作业内回环 | 外网 | 宿主 22 端口 | `mpirun -np 2` |
|---|---|---|---|---|
| bwrap（`--unshare-net`）| 通 | 不可达 | 拒绝 | `ranks=2 sum=3` |
| 纯 Landlock | 被拒 | 被拒 | 被拒 | PMIx 监听线程起不来 |

bwrap 的私有网络命名空间本身就是那个范围，不需要开口、不弹卡；纯 Landlock / seatbelt 守不住它，
不开口，在提交前说清。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

from core import isolation, sandbox


@pytest.fixture(autouse=True)
def _fresh_backend():
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


def _with(monkeypatch, loopback: str | None, backend: str | None = "linux") -> None:
    snapshot = {"backend": backend, "job_loopback": loopback,
                "unavailable_reason": None if backend else "no backend"}
    monkeypatch.setattr(isolation, "enforcement_snapshot", lambda: snapshot)


@pytest.mark.parametrize("loopback,runs,contained", [
    ("private_netns", True, True),
    ("host_shared", True, False),
    ("denied", False, False),
])
def test_the_fact_follows_what_the_wall_does(monkeypatch, loopback, runs, contained) -> None:
    _with(monkeypatch, loopback)
    cap = sandbox.job_tcp_capability()
    assert (cap.runs, cap.contained) == (runs, contained)
    assert cap.sentence().startswith("本机多进程通信")
    if not runs:
        assert "bubblewrap" in cap.reason and "提交之前" in cap.reason


def test_no_backend_is_said_not_guessed(monkeypatch) -> None:
    _with(monkeypatch, None, backend=None)
    cap = sandbox.job_tcp_capability()
    assert cap.runs is False and "起不来" in cap.reason


def test_every_backend_reports_it() -> None:
    """记账里有这一项（缺了就是 UI / 开局说明 / 提交前拒绝三处各自去猜）。"""
    try:
        snapshot = isolation.enforcement_snapshot()
    except Exception:
        pytest.skip("这台机器没有可用后端")
    if snapshot.get("backend"):
        assert snapshot.get("job_loopback") in {"private_netns", "denied", "host_shared"}


def test_the_opening_facts_say_it(monkeypatch) -> None:
    from core import execution_context_facts as ecf

    facts = ecf.gather(snapshot={"backend": "linux", "enforced": []}, platform="linux",
                       shell="/bin/bash", python="python3", os_text="Linux",
                       job_tcp="本机多进程通信（MPI 回环 TCP）：可以。")
    assert "本机多进程通信" in ecf.render_section(facts)


# ── 真过墙（只在有 bwrap 的 Linux 上；纯 Landlock 那一行靠强制关掉 bwrap）───────────


_PROBE = r'''
import socket, threading
out = {}
try:
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1); port = srv.getsockname()[1]
    threading.Thread(target=lambda: (lambda c: (c[0].sendall(b"pong"), c[0].close()))(srv.accept()),
                     daemon=True).start()
    cli = socket.socket(); cli.settimeout(5); cli.connect(("127.0.0.1", port)); out["loopback"] = cli.recv(4).decode()
except Exception as e:
    out["loopback"] = type(e).__name__
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5).close(); out["internet"] = "CONNECTED"
except Exception as e:
    out["internet"] = type(e).__name__
print(out)
'''


def _bwrap_linux() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        return isolation.enforcement_snapshot().get("job_loopback") == "private_netns"
    except Exception:
        return False


def _in_wall(*argv: str, shell: bool = False, work: Path | None = None) -> tuple[str, int | None, str, str]:
    from shared.lib.cancellable_subprocess import spawn_and_wait

    work = work or Path(tempfile.mkdtemp(prefix="hf-job-tcp-"))
    st = types.SimpleNamespace(kill_event=None, append_transcript=lambda *a, **k: None)
    status, rc, out, err = asyncio.run(spawn_and_wait(
        *argv, state=st, timeout=120, shell=shell, cwd=str(work), writable_roots=[work]))
    return status, rc, out.decode(errors="replace"), err.decode(errors="replace")


@pytest.mark.skipif(not _bwrap_linux(), reason="只在 bwrap 可用的 Linux 上")
def test_inside_the_wall_loopback_works_and_the_internet_does_not() -> None:
    status, rc, out, err = _in_wall(sys.executable, "-c", _PROBE)
    assert (status, rc) == ("done", 0), err
    assert "'loopback': 'pong'" in out, out
    assert "'internet': 'CONNECTED'" not in out, f"作业里连上了外网：{out}"


@pytest.mark.skipif(not (_bwrap_linux() and shutil.which("mpicc") and shutil.which("mpirun")),
                    reason="只在有 bwrap 与 MPI 的 Linux 上")
def test_a_two_rank_mpi_job_computes_inside_the_wall() -> None:
    # 编在作业自己的可写根里：bwrap 在 /tmp 上铺私有 tmpfs，没声明的 /tmp 路径墙里看不见。
    work = Path(tempfile.mkdtemp(prefix="hf-job-mpi-"))
    src = work / "hello.c"
    src.write_text(
        "#include <mpi.h>\n#include <stdio.h>\nint main(int c,char**v){MPI_Init(&c,&v);int r,s,m,t=0;"
        "MPI_Comm_rank(MPI_COMM_WORLD,&r);MPI_Comm_size(MPI_COMM_WORLD,&s);m=r+1;"
        "MPI_Allreduce(&m,&t,1,MPI_INT,MPI_SUM,MPI_COMM_WORLD);if(!r)printf(\"ranks=%d sum=%d\\n\",s,t);"
        "MPI_Finalize();return 0;}\n", encoding="utf-8")
    exe = work / "hello"
    subprocess.run(["mpicc", "-o", str(exe), str(src)], check=True)
    status, rc, out, err = _in_wall(f"mpirun --oversubscribe -np 2 {exe}", shell=True, work=work)
    assert (status, rc) == ("done", 0), err[-500:]
    assert "ranks=2 sum=3" in out, out
