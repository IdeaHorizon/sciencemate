"""本地作业认的是**进程**，不是号（#1085）。

裸 pid / pgid 不是身份，是号，而号会被复用。复用之后：

- `stop_container` 对记录里的组号 `killpg(SIGTERM)` —— 打的是无关进程组。而
  Experiment 在**每个本地作业正常收尾时**都会调一次 `stop_container`；实测：收尾一个
  `status=exited` 的作业，SIGTERM 发给了一个占号的无关进程组（返回还是 True）。
  平台自己的 worker 和命令正好是以组长身份起的，同样在射程内。
- `inspect_container` 把占号的进程当成原作业，于是早就结束的作业报 `running`。
- 作业因此走不出去：取消报 `local_job_stop_unconfirmed`，收尾报 `finalized_needs_cleanup`。

主机重启把这个概率放大：旧记录还在、状态停在 running，而 PID 计数从头开始。

判据分三档，**不能压成两档**：身份对上 / 号被复用 / 读不到身份。第三档要 fail
closed —— 既不判死，也不发信号。
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import pytest

from core import sandbox
from shared.lib import process_control


@pytest.fixture(autouse=True)
def _jobs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    (tmp_path / "jobs").mkdir(parents=True, exist_ok=True)
    yield


def _write_record(**fields) -> tuple[str, str]:
    name, rid = f"hf-job-{secrets.token_hex(8)}", secrets.token_hex(32)
    d = sandbox._jobs_root() / name
    d.mkdir(parents=True)
    record = {"name": name, "runtime_id": rid, "kind": "job", "status": "running"}
    record.update(fields)
    (d / "record.json").write_text(json.dumps(record), encoding="utf-8")
    return name, rid


# ── 出生身份本身 ────────────────────────────────────────────────────────────

def _a_different_identity_same_scale(pid: int) -> str:
    """同一把刻度上的**另一个**出生时刻 —— 用来扮演"号被复用了"。

    不能写死 `"start:1.000"`：刻度是随平台的（Linux 用 /proc 的 starttime 滴答，
    其它平台用 psutil 的秒），跨刻度比会答 None（不比），于是这条用例在另一个
    平台上什么也没验到 —— CI 上实测就是这么全绿着漏过去的。
    """
    current = process_control.birth_identity(pid)
    assert current, "读不出本进程的出生身份，这条用例没有前提"
    scale, value = current.split(":", 1)
    return f"{scale}:{'1' if value != '1' else '2'}"



def test_birth_identity_distinguishes_a_reused_number() -> None:
    mine = process_control.birth_identity(os.getpid())
    assert mine and ":" in mine
    assert process_control.identity_matches(os.getpid(), mine) is True
    assert process_control.identity_matches(
        os.getpid(), _a_different_identity_same_scale(os.getpid())) is False, (
        "别的出生时刻被当成同一个进程 —— 号复用就防不住了")


def test_an_unreadable_identity_is_not_a_mismatch() -> None:
    """读不到 ≠ 不是它。压成一个答案，fail closed 就没地方落了。"""
    assert process_control.identity_matches(os.getpid(), None) is None
    assert process_control.identity_matches(
        2_000_000, _a_different_identity_same_scale(os.getpid())) is False, (
        "进程根本不在，那就是肯定不是它")


# ── stop：发信号之前先问身份 ────────────────────────────────────────────────


def test_stop_checks_birth_identity_before_signal() -> None:
    """#1085 A 验收：号被复用时不发信号、不删账本，结构化地说出来。"""
    bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        name, rid = _write_record(
            status="running",
            pid=bystander.pid,                    # 号被这个无关进程占着
            group=f"pgid:{bystander.pid}",
            # 原作业的出生时刻，对不上（同一把刻度，否则跨刻度会答"不比"）
            birth_identity=_a_different_identity_same_scale(bystander.pid),
        )
        assert sandbox.stop_container(name, remove=True, expected_container_id=rid) is False
        assert bystander.poll() is None, "号被复用了还是把信号发出去了"

        control = sandbox._jobs_root() / name
        assert control.exists(), "号被复用时删了账本 —— 连「这条没收尾」都抹掉了"
        record = json.loads((control / "record.json").read_text(encoding="utf-8"))
        assert record["stop_refused"] == "pid_reused"
    finally:
        bystander.kill()
        bystander.wait()


def test_a_terminal_record_is_cleaned_up_without_any_signal() -> None:
    """账上已经是终态就只清理 —— 这一条消掉「正常收尾时误发信号」那整类。"""
    bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        name, rid = _write_record(
            status="exited", exit_code=0,
            pid=4_000_000,                        # 原 supervisor：早没了
            group=f"pgid:{bystander.pid}",        # 组号被无关进程组占着
        )
        assert sandbox.stop_container(name, remove=True, expected_container_id=rid) is True
        assert bystander.poll() is None, (
            "收尾一个已经结束的作业，SIGTERM 发给了无关进程组 —— 实测过的那一条")
        assert not (sandbox._jobs_root() / name).exists()
    finally:
        bystander.kill()
        bystander.wait()


def test_a_live_job_is_still_stopped() -> None:
    """对照：身份对得上的活作业照常停 —— 这条修复不能把闸变成永远不发信号。"""
    job = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        name, rid = _write_record(
            status="running", pid=job.pid, group=f"pgid:{job.pid}",
            birth_identity=process_control.birth_identity(job.pid),
        )
        assert sandbox.stop_container(name, remove=True, expected_container_id=rid) is True
        deadline = time.monotonic() + 5
        while job.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert job.poll() is not None, "身份对得上的作业没被停掉"
    finally:
        if job.poll() is None:
            job.kill()
        job.wait()


# ── inspect：号被复用不算还在跑 ─────────────────────────────────────────────


def test_inspect_does_not_call_a_reused_number_running() -> None:
    bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        name, _rid = _write_record(
            status="running", pid=bystander.pid,
            birth_identity=_a_different_identity_same_scale(bystander.pid))
        info = sandbox.inspect_container(name)
        assert info["running"] is False, "占号的无关进程把这个作业撑成了 running"
        assert info["status"] == "dead"
        assert info["identity"] == "pid_reused"
    finally:
        bystander.kill()
        bystander.wait()


def test_inspect_says_when_it_could_not_check_identity() -> None:
    """升级前起的作业没有出生身份 —— 退回裸 pid 判活，但要**如实标注**。"""
    job = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        name, _rid = _write_record(status="running", pid=job.pid)   # 没有 birth_identity
        info = sandbox.inspect_container(name)
        assert info["running"] is True
        assert info["identity"] == "unknown", "没查过身份却报得像查过了"
    finally:
        job.kill()
        job.wait()


def test_the_supervisor_records_its_birth_identity() -> None:
    """判据要有来源：起作业时就把出生身份写进 record。

    组号不另设锚：只要 supervisor 还活着，它就是那个组的成员，组不会空、组号就
    不会被重新分配 —— 所以核 supervisor 的出生身份**等价于**核了组号。
    """
    src = Path(__file__).resolve().parents[1] / "core/isolation/_native_job.py"
    body = src.read_text(encoding="utf-8")
    assert "birth_identity" in body, "record 里没有出生身份 —— 后面比什么都比不了"


# ── 收尸：owner 有且只有一个 ────────────────────────────────────────────────


def test_terminal_supervisor_has_one_assigned_reaping_owner() -> None:
    """#1085 B 验收：收尸责任方**有且只有一个**，而且写在代码里不是注释里。

    supervisor 是双 fork 出来的孤儿（这是有意的：后端收摊/换代不该毁掉正在跑的
    研究），所以框架里没有任何进程能对它 waitpid —— owner 只能是部署方的 PID 1。
    一个没有 owner 的义务，会被每一方都当成别人的事。
    """
    assert process_control.ORPHAN_REAPING_OWNER == "deployment_pid1"

    from core.isolation import enforcement_record
    from core.isolation.linux import LinuxBackend

    class _B:
        name = "fake"

        def capabilities(self):
            return frozenset()

    fact = enforcement_record(_B()).as_event()["orphan_reaping"]
    assert fact["owner"] == "deployment_pid1"
    assert fact["pid1_reaps"] in (True, False), (
        "只声明 owner 不够 —— 这台机器满不满足那个前提，必须是探出来的事实")
    assert LinuxBackend is not None      # 只为说明这条与后端无关，三平台同一份


@pytest.mark.skipif(
    not process_control.orphans_get_reaped(),
    reason="这台机器的 PID 1 不收割孤儿僵尸（容器没带 --init）—— 跳过本身就是"
           "「部署方那条前提没满足」的证据，见 enforcement_record.orphan_reaping",
)
def test_terminal_supervisor_is_reaped_within_bound() -> None:
    """作业进入终态后，限定时间内不该还有 `Z` 状态的孤儿留在进程表里。

    判据直接问**僵尸状态**，不问"进程组还在不在" —— 后者要 `killpg(pgid, 0)`，
    号一旦被别的用户的组复用就是 EPERM，于是"看不了"和"还在"压成同一个答案
    （这条判据的第一版就是这么在收集阶段抛出来的）。
    """
    import psutil

    probe = subprocess.run(
        ["/bin/sh", "-c", "sleep 0.05 </dev/null >/dev/null 2>&1 & echo $!"],
        capture_output=True, text=True, start_new_session=True, check=False)
    orphan = int((probe.stdout or "").strip() or 0)
    assert orphan > 0, "探针没报出孤儿 pid，这条用例什么也没验到"

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            status = psutil.Process(orphan).status()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return                      # 进程表里没有它了 = 被收走了
        if status != psutil.STATUS_ZOMBIE:
            time.sleep(0.05)
            continue
        time.sleep(0.05)
    pytest.fail("终态进程 3 秒后仍以僵尸留在进程表里 —— 没有人收尸")


def test_the_identity_scale_is_stable_under_clock_adjustment() -> None:
    """Linux 上的出生身份不能依赖 `boot_time` —— NTP 一调时它就会漂。

    `create_time()` 在 Linux 上是 `boot_time + starttime/HZ`，而 `boot_time` 读的是
    `/proc/stat` 的 `btime`。CI（容器里跑 NTP）实测：作业刚起来就被判成
    `pid_reused`、status 报 dead。starttime 是相对开机的滴答数，不经过 btime。
    """
    import sys

    identity = process_control.birth_identity(os.getpid())
    assert identity
    if sys.platform.startswith("linux"):
        assert identity.startswith("ticks:"), (
            f"Linux 上仍在用会随 NTP 漂的刻度：{identity!r}")
    else:
        assert identity.startswith("start:")


def test_two_different_scales_are_not_compared() -> None:
    """换过口径 / 跨平台搬过账本 → 不比，答 None。

    拿滴答数和秒去比会得出"不是同一个进程"—— 那是个假答案，而假答案会让一个
    活着的作业被判死。
    """
    current = process_control.birth_identity(os.getpid())
    other_scale = "ticks:999" if current.startswith("start:") else "start:1.000"
    assert process_control.identity_matches(os.getpid(), other_scale) is None
