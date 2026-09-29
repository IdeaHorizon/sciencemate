"""原生后端（darwin seatbelt / linux Landlock+bwrap+cgroup）的契约测试。

这套测试对**这台机器上能起来的原生后端**真跑：真写、真连、真超时、真留孤儿，
断言落在效果上，不落在 argv 长什么样上。同一份契约后续也给 wsl / windows 跑。

每条对应一条不变量；后端声明了哪条就验哪条，没声明的那条验「记账里确实缺着」——
不声明不等于没测，是另一种判据（[[feedback_absent_check_looks_like_passed_check]]）。

变异判据：注释掉 darwin.py 里 ``(deny network*)`` 那行，网络那条红；去掉
``layers.git`` 那条 deny，``.git`` 那条红（bwrap / seatbelt）；Landlock-only 主机上把
``linux.py::prepare`` 的结构拒绝反过来，``.git`` 那条红；去掉咽喉里命令结束后的整组清扫，
孤儿那条红。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import isolation
from core.isolation import Invariant, select_backend
from shared.lib.cancellable_subprocess import spawn_and_wait

NATIVE = "darwin" if sys.platform == "darwin" else "linux"
_backend = select_backend(NATIVE)
_caps = _backend.capabilities()
_reason = getattr(_backend, "unavailable_reason", "")

pytestmark = pytest.mark.skipif(
    Invariant.WRITE_BOUNDARY not in _caps,
    reason=f"native backend {NATIVE} cannot enforce the write boundary here: {_reason}",
)


class _State:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.kill_event = None

    def append_transcript(self, event_type: str, **payload) -> None:
        self.events.append({"event": event_type, **payload})


@pytest.fixture(autouse=True)
def _native(monkeypatch):
    monkeypatch.setenv(isolation.EXECUTOR_ENV, NATIVE)
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


@pytest.fixture()
def roots(tmp_path: Path) -> dict[str, Path]:
    """pytest 的 tmp_path 住在 TMPDIR 之下，而 TMPDIR 是 scratch、本来就宽放行 ——
    这正是 PR #431 撞过的形状：worktree 恰好在 /tmp 下。所以这里把整个 tmp_path 当
    worktree（拒写层），mine/ 与 run/ 是压回可写的自己的根，other/ 是别人的节点目录。
    """
    worktree = tmp_path
    (worktree / "mine").mkdir()
    (worktree / "other").mkdir()
    (worktree / "run").mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    return {"worktree": worktree, "mine": worktree / "mine", "other": worktree / "other",
            "run": worktree / "run"}


def _sh(cmd: str, roots: dict[str, Path], *, timeout: float = 30, state=None, **kw):
    state = state or _State()
    return state, asyncio.run(spawn_and_wait(
        cmd, state=state, timeout=timeout, shell=True, cwd=str(roots["mine"]),
        writable_roots=[roots["mine"], roots["run"]], readonly_roots=[roots["worktree"]],
        **kw,
    ))


def test_write_boundary_own_dirs_writable_everything_else_not(roots) -> None:
    _, (status, rc, _out, err) = _sh(
        f"echo a > '{roots['mine']}/a' && echo b > '{roots['run']}/b' && echo ok", roots)
    assert (status, rc) == ("done", 0), err
    assert (roots["mine"] / "a").read_text() == "a\n"
    assert (roots["run"] / "b").read_text() == "b\n"

    # 别人的节点目录、worktree 根：在拒写层里，写不进
    for name in ("other", "worktree"):
        target = roots[name] / "x"
        _, (status, rc, _out, err) = _sh(f"echo x > '{target}'", roots)
        assert status == "done" and rc != 0, f"{name}: 写出去了"
        assert not target.exists(), f"{name}: 文件真被写出来了"

    # scratch 之外、worktree 之外的宿主目录（$HOME）：默认全拒
    home_target = Path.home() / f".hf-isolation-probe-{os.getpid()}"
    try:
        _, (status, rc, _out, err) = _sh(f"echo x > '{home_target}'", roots)
        assert status == "done" and rc != 0, "写进 $HOME 了"
        assert not home_target.exists()
    finally:
        home_target.unlink(missing_ok=True)


def test_scratch_stays_writable_so_science_libraries_do_not_scream(roots) -> None:
    # 命令看到的 $TMPDIR 必须可写（seatbelt / bwrap 放行共享 tmp；Landlock 给私有
    # scratch 并把 TMPDIR 指过去）—— 判据是命令自己的 TMPDIR，不是测试进程的。
    _, (status, rc, out, err) = _sh(
        'd="${TMPDIR:-/tmp}"; echo s > "$d/hf-scratch-probe" && cat "$d/hf-scratch-probe" '
        '&& rm -f "$d/hf-scratch-probe"', roots)
    assert (status, rc) == ("done", 0), err
    assert out.strip() == b"s"


#: Linux 上没有 bwrap = 只有 Landlock。它没有 deny 层，.git 只能靠"不接"。
_LANDLOCK_ONLY = NATIVE == "linux" and getattr(_backend, "_bwrap", None) is None


def test_git_directory_under_a_writable_root_is_not_writable(roots) -> None:
    # 把整个 worktree 当可写根，.git 仍然写不进去。两种守法，两种长相：
    #   · bwrap / seatbelt 有 deny 层：命令照跑，写 .git 那一步被拒（rc != 0）
    #   · 只有 Landlock：纯放行清单挡不住根下的 .git —— 于是**不接这单**：派发被拒并
    #     说清该传节点目录（spawn_failed）。守不住就不装守住。
    state = _State()
    status, rc, _out, err = asyncio.run(spawn_and_wait(
        f"echo x > '{roots['worktree']}/.git/HACK'", state=state, timeout=30, shell=True,
        cwd=str(roots["worktree"]), writable_roots=[roots["worktree"]], readonly_roots=[]))
    assert not (roots["worktree"] / ".git" / "HACK").exists(), ".git 被写了"
    if Invariant.GIT_UNWRITABLE not in _caps:
        record = [e for e in state.events if e["event"] == "isolation_enforcement"][0]
        assert "git_unwritable" in record["missing_for_attended"], "守不到就得记在账上"
    elif _LANDLOCK_ONLY:
        assert status == "spawn_failed", (status, rc, err)
        assert b"GIT_UNWRITABLE" in err, err
    else:
        assert status == "done" and rc != 0


def test_network_is_denied_for_model_commands(roots) -> None:
    # seatbelt 给 EPERM（PermissionError）；bwrap --unshare-net 给 ENETUNREACH（没有路由）。
    # 两种都是「被拒」；连上 / 超时 / ECONNREFUSED 都是「有网」。目标用公网 IP 是因为
    # 127.0.0.1 上没人听端口时不加沙箱也会 ECONNREFUSED，分不出真假。
    probe = (
        "import errno, socket, sys\n"
        "s=socket.socket(); s.settimeout(3)\n"
        "try:\n    s.connect(('1.1.1.1', 53))\n"
        "except PermissionError:\n    sys.exit(0)\n"
        "except OSError as e:\n"
        "    sys.exit(0 if e.errno in (errno.ENETUNREACH, errno.EACCES, errno.EPERM) else 3)\n"
        "sys.exit(4)\n"
    )
    state = _State()
    status, rc, _out, err = asyncio.run(spawn_and_wait(
        sys.executable, "-I", "-c", probe, state=state, timeout=30,
        cwd=str(roots["mine"]), writable_roots=[roots["mine"]], readonly_roots=[]))
    assert status == "done", err
    if Invariant.NET_DENY in _caps:
        assert rc == 0, f"connect 没被拒（rc={rc}）: {err!r}"
    else:
        record = [e for e in state.events if e["event"] == "isolation_enforcement"][0]
        assert "net_deny" in record["missing_for_attended"]


def test_walltime_kills_the_command(roots) -> None:
    started = time.monotonic()
    _, (status, _rc, _out, _err) = _sh("sleep 30", roots, timeout=1)
    assert status == "timeout"
    assert time.monotonic() - started < 15, "超时后收场太慢"


def test_background_children_die_when_the_command_returns(roots) -> None:
    _, (status, rc, out, err) = _sh("sleep 300 & echo $!", roots)
    assert (status, rc) == ("done", 0), err
    child = int(out.decode().strip().splitlines()[-1])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(child)],
                              capture_output=True, text=True).stdout.strip()
        if not stat or stat.startswith("Z"):
            return
        time.sleep(0.1)
    subprocess.run(["kill", "-9", str(child)], check=False)
    pytest.fail(f"后台 sleep（pid {child}）在命令返回后还活着：stat={stat!r}")


def test_secrets_in_the_host_environment_do_not_reach_the_model(roots, monkeypatch) -> None:
    monkeypatch.setenv("HF_TEST_API_KEY", "sk-should-not-leak-1234567890")
    monkeypatch.setenv("HF_TEST_PLAIN", "visible")
    _, (status, rc, out, err) = _sh("env", roots)
    assert (status, rc) == ("done", 0), err
    text = out.decode()
    assert "HF_TEST_PLAIN=visible" in text, "普通环境变量应该原样给模型"
    assert "sk-should-not-leak" not in text, "凭据泄进了模型的 shell"
    assert "PATH=" in text


def test_a_kill_event_from_a_dead_loop_is_not_a_cancel(roots) -> None:
    """同一个 state 跨两次 asyncio.run：kill_event 绑在第一个 loop 上，第二次 wait 立刻抛
    RuntimeError。这不是取消 —— 以前咽喉把它当取消，把健康命令 SIGKILL 成 rc=-9
    （writing 的 sci_project 双次编译真撞了）。"""
    state = _State()

    async def first():
        state.kill_event = asyncio.Event()  # 绑在这个 loop 上
        return await spawn_and_wait("echo one", state=state, timeout=30, shell=True,
                                    cwd=str(roots["mine"]), writable_roots=[roots["mine"]],
                                    readonly_roots=[roots["worktree"]])

    async def second():
        return await spawn_and_wait("echo two", state=state, timeout=30, shell=True,
                                    cwd=str(roots["mine"]), writable_roots=[roots["mine"]],
                                    readonly_roots=[roots["worktree"]])

    assert asyncio.run(first())[:2] == ("done", 0)
    status, rc, out, _err = asyncio.run(second())
    assert (status, rc) == ("done", 0), "坏掉的停止信号被当成了取消"
    assert out.strip() == b"two"


def test_enforcement_record_names_this_backend_and_its_gaps(roots) -> None:
    state, (status, _rc, _out, _err) = _sh("true", roots)
    assert status == "done"
    records = [e for e in state.events if e["event"] == "isolation_enforcement"]
    assert len(records) == 1
    record = records[0]
    assert record["backend"] == NATIVE
    assert set(record["enforced"]) == {c.value for c in _caps}
    # 写边界是唯一硬性前提；.git 与断网在没有 bwrap 的 Linux（只有 Landlock）上守不到，
    # 那就必须**记在账上**，不许假装。darwin 的 seatbelt 三样都有。
    assert "write_boundary" not in record["missing_for_attended"]
    assert set(record["missing_for_attended"]) <= {"git_unwritable", "net_deny"}
    if NATIVE == "darwin":
        assert record["missing_for_attended"] == []
    assert json.dumps(record)
