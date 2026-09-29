"""win32 原生后端（Low IL 令牌 + Job Object）的真机契约测试。

``skipif`` 非 win32 —— 只在 tailnet 的 Windows 机器上真跑：真降令牌、真打标签、真写一次、
真装满 job、真超时。断言落在效果上（文件写没写出来、进程死没死），不落在 argv 长相上。
每条对应一条不变量；后端声明了哪条就验哪条，没声明的（NET_DENY）验「记账里确实缺着」——
不声明不等于没测，是另一种判据（[[feedback_absent_check_looks_like_passed_check]]）。

用 **python 载荷**而不是 bash 载荷来验写边界：Windows 路径进 MSYS bash 要处理反斜杠转义，
那是 ``the_shell`` 的地界；这里要验的是**墙**，用 ``python open()`` 直接对绝对路径写，
噪音最小，也和 darwin 的 network 探针（同样走 ``-c`` 载荷）一个路子。

变异判据（在 Windows 上真跑）：把 ``_win32_exec._low_il_primary_token`` 的 SetTokenInformation
去掉（不降 IL），写边界那条红；把 ``run()`` 里 ``.git`` 的 ``_icacls(..., "M")`` 改成 ``"L"``，
git 那条红；把 ``_make_job`` 的 ``KILL_ON_JOB_CLOSE`` 去掉，后台子进程那条红。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from core import isolation
from core.isolation import Invariant, select_backend
from shared.lib.cancellable_subprocess import spawn_and_wait

_backend = select_backend("win32")
_caps = _backend.capabilities()
_reason = getattr(_backend, "unavailable_reason", "")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or Invariant.WRITE_BOUNDARY not in _caps,
    reason=f"win32 backend cannot enforce the write boundary here: {_reason or sys.platform}",
)


class _State:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.kill_event = None

    def append_transcript(self, event_type: str, **payload) -> None:
        self.events.append({"event": event_type, **payload})


@pytest.fixture(autouse=True)
def _win32(monkeypatch):
    monkeypatch.setenv(isolation.EXECUTOR_ENV, "win32")
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


@pytest.fixture()
def roots(tmp_path: Path) -> dict[str, Path]:
    """tmp_path 住在 %TEMP% 之下。把整个 tmp_path 当 worktree（不打标签 = Medium = 拒写），
    mine/ 与 run/ 是打 Low 标签压回可写的自己的根，other/ 是别人的节点目录。``.git`` 用真目录
    （不依赖测试环境有 git.exe —— write_layers 只看它 exists，launcher 给它打 Medium）。
    """
    worktree = tmp_path
    for name in ("mine", "other", "run"):
        (worktree / name).mkdir()
    (worktree / ".git").mkdir()
    (worktree / ".git" / "keep").write_text("real .git\n", encoding="utf-8")
    return {"worktree": worktree, "mine": worktree / "mine", "other": worktree / "other",
            "run": worktree / "run"}


def _write_probe(target: Path) -> str:
    """一段 python：往 target 写一个字节，写成退 0，被 OS 拒退 7，其它错退 3。"""
    return (
        "import sys\n"
        "try:\n"
        f"    open(r'{target}', 'w', encoding='utf-8').write('x')\n"
        "except PermissionError:\n"
        "    sys.exit(7)\n"
        "except OSError:\n"
        "    sys.exit(3)\n"
        "sys.exit(0)\n"
    )


def _run(program: str, *, writable, readonly, cwd, timeout: float = 30, state=None):
    state = state or _State()
    return state, asyncio.run(spawn_and_wait(
        sys.executable, "-I", "-c", program, state=state, timeout=timeout,
        cwd=str(cwd), writable_roots=list(writable), readonly_roots=list(readonly),
    ))


def test_write_boundary_own_dirs_writable_everything_else_not(roots) -> None:
    writable = [roots["mine"], roots["run"]]
    readonly = [roots["worktree"]]

    for name in ("mine", "run"):
        target = roots[name] / "a"
        _, (status, rc, _out, err) = _run(_write_probe(target), writable=writable,
                                          readonly=readonly, cwd=roots["mine"])
        assert (status, rc) == ("done", 0), f"{name}: 自己的根写不进 ({err!r})"
        assert target.read_text(encoding="utf-8") == "x"

    # 别人的节点目录、worktree 根（没打 Low 标签 = Medium）：Low-IL 写不进
    for name in ("other", "worktree"):
        target = roots[name] / "x"
        _, (status, rc, _out, err) = _run(_write_probe(target), writable=writable,
                                          readonly=readonly, cwd=roots["mine"])
        assert status == "done" and rc != 0, f"{name}: 写出去了 (rc={rc})"
        assert not target.exists(), f"{name}: 文件真被写出来了"

    # worktree / scratch 之外的宿主目录（用户 profile 根）：默认全拒
    home_target = Path.home() / f".hf-win32-probe-{id(roots)}"
    try:
        _, (status, rc, _out, _err) = _run(_write_probe(home_target), writable=writable,
                                           readonly=readonly, cwd=roots["mine"])
        assert status == "done" and rc != 0, "写进用户 profile 根了"
        assert not home_target.exists()
    finally:
        home_target.unlink(missing_ok=True)


def test_scratch_stays_writable_so_science_libraries_do_not_scream(roots) -> None:
    # 命令看到的 %TEMP% 必须可写（win32 给私有 scratch 并把 TMP/TEMP/TMPDIR 指过去）——
    # 判据是命令自己的 TEMP，不是测试进程的。
    program = (
        "import os, sys\n"
        "d = os.environ.get('TEMP') or os.environ.get('TMP') or os.environ.get('TMPDIR')\n"
        "p = os.path.join(d, 'hf-scratch-probe')\n"
        "open(p, 'w', encoding='utf-8').write('s')\n"
        "sys.stdout.write(open(p, encoding='utf-8').read())\n"
        "os.remove(p)\n"
    )
    _, (status, rc, out, err) = _run(program, writable=[roots["mine"]], readonly=[],
                                     cwd=roots["mine"])
    assert (status, rc) == ("done", 0), err
    assert out.decode().strip() == "s"


def test_git_directory_under_a_writable_root_is_not_writable(roots) -> None:
    # 把整个 worktree 当可写根（平台 attempt 就是这么冻的），.git 仍然写不进去
    target = roots["worktree"] / ".git" / "HACK"
    state, (status, rc, _out, err) = _run(_write_probe(target), writable=[roots["worktree"]],
                                          readonly=[], cwd=roots["worktree"])
    assert status == "done", err
    if Invariant.GIT_UNWRITABLE in _caps:
        assert rc != 0, ".git 被写进去了"
        assert not target.exists(), ".git/HACK 真被写出来了"
    else:
        record = [e for e in state.events if e["event"] == "isolation_enforcement"][0]
        assert "git_unwritable" in record["missing_for_attended"], "守不到就得记在账上"


def test_network_is_missing_from_the_record_not_silently_enforced(roots) -> None:
    # Windows 上没有免管理员的断网原语 —— NET_DENY 必须**缺在账上**，不许假装守到了。
    state, (status, _rc, _out, _err) = _run("import sys; sys.exit(0)",
                                            writable=[roots["mine"]], readonly=[],
                                            cwd=roots["mine"])
    assert status == "done"
    record = [e for e in state.events if e["event"] == "isolation_enforcement"][0]
    assert Invariant.NET_DENY not in _caps
    assert "net_deny" in record["missing_for_unattended"], "断网守不到就得记在账上"


def test_walltime_kills_the_command(roots) -> None:
    started = time.monotonic()
    _, (status, _rc, _out, _err) = _run("import time; time.sleep(30)",
                                        writable=[roots["mine"]], readonly=[roots["worktree"]],
                                        cwd=roots["mine"], timeout=1)
    assert status == "timeout"
    assert time.monotonic() - started < 15, "超时后收场太慢"


def test_background_children_die_when_the_command_returns(roots) -> None:
    # Job 的 KILL_ON_JOB_CLOSE：启动器持 job 句柄，命令返回→启动器退出→句柄关→整树亡。
    # 载荷起一个后台 python 长睡、打印它的 pid、自己立刻返回（不等它）。
    import psutil

    program = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(300)'])\n"
        "print(p.pid, flush=True)\n"
    )
    _, (status, rc, out, err) = _run(program, writable=[roots["mine"]],
                                     readonly=[roots["worktree"]], cwd=roots["mine"])
    assert (status, rc) == ("done", 0), err
    child = int(out.decode().strip().splitlines()[-1])
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not psutil.pid_exists(child):
            return
        proc = psutil.Process(child) if psutil.pid_exists(child) else None
        if proc is None or proc.status() == psutil.STATUS_ZOMBIE:
            return
        time.sleep(0.1)
    try:
        psutil.Process(child).kill()
    except psutil.Error:
        pass
    pytest.fail(f"后台 sleep（pid {child}）在命令返回后还活着")


def test_secrets_in_the_host_environment_do_not_reach_the_model(roots, monkeypatch) -> None:
    monkeypatch.setenv("HF_TEST_API_KEY", "sk-should-not-leak-1234567890")
    monkeypatch.setenv("HF_TEST_PLAIN", "visible")
    program = ("import os, sys\n"
               "sys.stdout.write('\\n'.join(f'{k}={v}' for k, v in os.environ.items()))\n")
    _, (status, rc, out, err) = _run(program, writable=[roots["mine"]], readonly=[],
                                     cwd=roots["mine"])
    assert (status, rc) == ("done", 0), err
    text = out.decode()
    assert "HF_TEST_PLAIN=visible" in text, "普通环境变量应该原样给模型"
    assert "sk-should-not-leak" not in text, "凭据泄进了模型的 shell"
    assert "PATH=" in text.upper()


def test_enforcement_record_names_this_backend_and_its_gaps(roots) -> None:
    state, (status, _rc, _out, _err) = _run("import sys; sys.exit(0)",
                                            writable=[roots["mine"]], readonly=[roots["worktree"]],
                                            cwd=roots["mine"])
    assert status == "done"
    records = [e for e in state.events if e["event"] == "isolation_enforcement"]
    assert len(records) == 1
    record = records[0]
    assert record["backend"] == "win32"
    assert set(record["enforced"]) == {c.value for c in _caps}
    assert "write_boundary" not in record["missing_for_attended"]
    # Windows 守不到断网；.git / 资源墙这台机器上守得到（§11 五测全绿）。
    assert set(record["missing_for_attended"]) <= {"net_deny"}
    assert json.dumps(record)
