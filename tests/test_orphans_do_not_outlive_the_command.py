"""调用结束了，工作还没停 —— 这不许发生（#1094）。

咽喉在命令结束时对顶层进程组 ``killpg``。模型命令里 ``setsid``（或 bash ``set -m``）起的后代
不在那个组里：``run_bash`` 已经返回 success / timeout / cancelled，几秒后它们还在可写根里写
文件，还能活过 harness 本身。有 systemd --user scope 的机器靠停 slice 整片收（cgroup 不在乎进程
组）；没有 scope 的 Linux（#1094 现场）和 macOS 上此前没有任何兜底。

现在每条模型命令的环境带一个本次独有的 ``HF_LAUNCH_ID``；后代继承它；调用结束、没有 cgroup
可停时，咽喉按它把还活着的全部收掉，并在 transcript 记一条 ``orphans_reaped``。

三条判据：
1. 原语：带标记的 ``setsid`` 孤儿被收，没带标记的同形进程不被误杀。
2. 真入口：``spawn_and_wait`` 跑一条会留下 setsid 后代的命令，返回后那个后代几秒内必须不在
   —— 不管这台机器是 cgroup 收的还是标记收的。
3. 账：标记收尾发生过就有 ``orphans_reaped`` 事件；什么都没收就没有这条（别制造幽灵事件）。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from core import isolation
from shared.lib import process_control

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows 走 Job 对象整组收")

#: 起一个 setsid 的孙进程（新会话、新进程组），打印它的 pid，然后自己退出 —— 孙进程成了孤儿。
#: 孙进程的 stdio 指向 /dev/null：否则它攥着父进程的管道，`subprocess.run(capture_output=True)`
#: 要等管道 EOF 才返回（咽喉本身等的是进程退出不是 EOF，所以真入口不受这条影响）。
_ORPHANER = (
    "import subprocess, sys\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],"
    " start_new_session=True, stdin=subprocess.DEVNULL,"
    " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print(p.pid, flush=True)\n"
)


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not process_control.alive(pid):
            return True
        time.sleep(0.05)
    return not process_control.alive(pid)


def _reap(*pids: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _spawn_orphan(env: dict[str, str]) -> int:
    out = subprocess.run([sys.executable, "-c", _ORPHANER], env=env,
                         capture_output=True, text=True, check=True, timeout=30)
    return int(out.stdout.strip().splitlines()[-1])


def test_kill_marked_reaps_the_marked_orphan_and_spares_the_bystander() -> None:
    marker = process_control.new_launch_marker()
    clean = {k: v for k, v in os.environ.items() if k != process_control.LAUNCH_MARKER}
    orphan = _spawn_orphan({**clean, process_control.LAUNCH_MARKER: marker})
    bystander = _spawn_orphan(clean)
    try:
        assert process_control.alive(orphan) and process_control.alive(bystander)
        victims = process_control.kill_marked(marker)
        assert orphan in victims, victims
        assert _gone_within(orphan, 3.0), "带标记的 setsid 孤儿没被收掉"
        assert process_control.alive(bystander), "没带标记的进程被误杀 —— 标记必须是本次独有的"
        assert process_control.kill_marked(marker) == [], "第二遍什么都不该再杀到"
    finally:
        _reap(orphan, bystander)


class _State:
    kill_event = None

    def __init__(self) -> None:
        self.events: list[dict] = []

    def append_transcript(self, event_type, **payload):
        self.events.append({"event": event_type, **payload})


def test_a_setsid_descendant_does_not_outlive_the_call(tmp_path, monkeypatch) -> None:
    """走真入口。这台机器有 cgroup 就由 cgroup 收，没有就由标记收 —— 结论一样：后代不在了。"""
    from shared.lib.cancellable_subprocess import spawn_and_wait

    isolation._reset_for_tests()
    backend = isolation.select_backend()
    why = getattr(backend, "unavailable_reason", "")
    if why:
        pytest.skip(f"这台机器没有可用的原生后端：{why}")
    mine = tmp_path / "mine"
    mine.mkdir()
    state = _State()
    try:
        status, rc, out, err = asyncio.run(spawn_and_wait(
            sys.executable, "-c", _ORPHANER, state=state, timeout=60,
            cwd=str(mine), writable_roots=[mine], readonly_roots=[]))
        assert status == "done" and rc == 0, (status, rc, err)
        orphan = int(out.decode().strip().splitlines()[-1])
        try:
            assert _gone_within(orphan, 5.0), (
                f"调用已经返回，setsid 出去的后代 {orphan} 还活着（#1094）")
        finally:
            _reap(orphan)
        reaped = [e for e in state.events if e["event"] == "orphans_reaped"]
        if sys.platform == "darwin":
            # macOS 没有 cgroup，一定是标记收的 —— 账上必须有这一条，而且数得对。
            assert reaped and reaped[0]["count"] >= 1 and orphan in reaped[0]["pids"], state.events
    finally:
        isolation._reset_for_tests()


def test_a_command_that_leaves_nothing_behind_records_nothing(tmp_path) -> None:
    """没收到任何东西就没有 orphans_reaped —— 别制造幽灵事件。"""
    from shared.lib.cancellable_subprocess import spawn_and_wait

    isolation._reset_for_tests()
    backend = isolation.select_backend()
    if getattr(backend, "unavailable_reason", ""):
        pytest.skip("这台机器没有可用的原生后端")
    mine = tmp_path / "mine"
    mine.mkdir()
    state = _State()
    try:
        status, rc, _out, err = asyncio.run(spawn_and_wait(
            sys.executable, "-c", "print('quiet')", state=state, timeout=60,
            cwd=str(mine), writable_roots=[mine], readonly_roots=[]))
        assert (status, rc) == ("done", 0), (status, rc, err)
        assert not [e for e in state.events if e["event"] == "orphans_reaped"], state.events
    finally:
        isolation._reset_for_tests()


def test_the_marker_is_unique_per_call_and_reaches_the_payload(tmp_path) -> None:
    """两条命令两个值；载荷自己看得见自己的标记（后代就是靠它被认出来的）。"""
    from shared.lib.cancellable_subprocess import spawn_and_wait

    isolation._reset_for_tests()
    backend = isolation.select_backend()
    if getattr(backend, "unavailable_reason", ""):
        pytest.skip("这台机器没有可用的原生后端")
    mine = tmp_path / "mine"
    mine.mkdir()
    seen = []
    try:
        for _ in range(2):
            status, rc, out, err = asyncio.run(spawn_and_wait(
                sys.executable, "-c",
                f"import os; print(os.environ.get({process_control.LAUNCH_MARKER!r}, ''))",
                state=_State(), timeout=60, cwd=str(mine), writable_roots=[mine], readonly_roots=[]))
            assert (status, rc) == ("done", 0), (status, rc, err)
            seen.append(out.decode().strip())
        assert all(len(v) == 32 for v in seen), f"载荷环境里没有标记：{seen}"
        assert seen[0] != seen[1], "两条命令共用一个标记 = 第二条结束时会把第一条的后代也当成自己的"
    finally:
        isolation._reset_for_tests()
