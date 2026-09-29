"""判成"无主"就要让它真的无主 —— 光改一行状态，进程还攥着 session 锁。

## 现场（2026-08-11）

重启 App Server 之后，用户在自己的会话里发消息，只得到：

    execution_failed: project session is already running

而库里那条 run 明明写着 `failed`。真相是：

    pid 57214  /…/python -m platform_runtime --serve   已活 2 小时 37 分
    fd 10w → …/session/…/.chat.lock                    (0 字节)

## 链条

给在途请求发 SIGTERM 之后，uvicorn 的优雅关闭**先关监听 socket，再等在途
请求结束**。而那个在途请求正等着 harness worker：

    老 App Server 永不退出 → worker 的 stdin 永不 EOF
      → worker 永不 close() → flock 永不释放
        → 新 App Server 能绑端口（老的已放开监听），把 run 标成 stale_unknown
          → 但那只是**账面**判决，进程还在
            → 下一个请求撞锁，错误信息还不肯说是谁攥着

于是「这个 session 忙不忙」有了两个真相源 —— 库里的 run 状态，和进程里的
flock。两边分叉时**谁都不报错**，要等下一个请求才炸。

## 两处修法都在这里验

1. 锁文件自报家门，且**抢锁失败不会把它抹掉**（原来用 `open("w")`，来抢的人
   先截断文件 —— 现场那个锁文件 0 字节就是这么来的）。
2. `mark_orphaned_harness_runs` 判完之后回收进程，让判决成立。

## 为什么这些测试要起真进程

锁路径的规则在两处各写了一遍（一处在 App Server，一处在子进程里，中间只有
JSONL，没法共享代码）。**对着复刻的判据测等于自己给自己打分** —— 所以这里
起真的进程、用 `platform_runtime` 里真的 `_project_lock` 拿真的 flock，再让
App Server 侧那份路径推导去找它。两边写岔了，这些测试就红。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.services.run_liveness import runtime_lost

HARNESS_ROOT = Path(__file__).resolve().parents[3]


def _load_platform_runtime():
    """按文件位置加载真实的 runtime 模块。

    不往 `sys.path` 里塞 harness 根目录 —— 那里有 `tests/`、`platform/` 这些
    会和后端自己的东西撞名的顶层项。这里要的只是"用真的那份 `_project_lock`"。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "platform_runtime_under_test", HARNESS_ROOT / "platform_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_runtime = _load_platform_runtime()

#: 起一个"看起来就是 worker"的进程：命令行里同时有 platform_runtime 和 --serve，
#: 并且用**真实的** `_project_lock` 去拿锁。它拿到锁就停在那儿，等着被回收。
_HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
import platform_runtime
with platform_runtime._project_lock(__import__("pathlib").Path(sys.argv[2])):
    sys.stderr.write("locked\\n"); sys.stderr.flush()
    time.sleep(120)
"""


def _spawn_holder(state_root: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(HARNESS_ROOT), str(state_root), "--serve"],
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stderr is not None
    line = proc.stderr.readline()          # 等它真的拿到锁再往下走
    assert "locked" in line, f"持有者没拿到锁：{line!r} (exit={proc.poll()})"
    return proc


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    """一个假的 Session worktree，让 App Server 侧的路径推导有地方落。"""
    from app.config import settings

    root = tmp_path / "worktrees"
    monkeypatch.setattr(settings, "project_worktree_root", str(root))
    path = root / "proj-1" / "sess-1"
    path.mkdir(parents=True)
    return path


def _state_root(worktree: Path) -> Path:
    return (
        worktree / ".research" / "runtime" / "runs"
        / "orchestrator__proj-1__session__sess-1"
    )


def test_the_app_server_finds_the_file_the_runtime_actually_locked(worktree) -> None:
    """两处路径推导必须指向同一个文件 —— 写岔了这里就红。"""
    from app.services.harness_sessions import _session_lock_path

    proc = _spawn_holder(_state_root(worktree))
    try:
        derived = _session_lock_path("proj-1", "sess-1")
        assert derived is not None and derived.is_file(), (
            f"App Server 推出来的路径 {derived} 不是 runtime 真正锁的那个文件"
        )
        record = json.loads(derived.read_text(encoding="utf-8"))
        assert record["pid"] == proc.pid, f"锁文件里的持有者不是它：{record}"
        assert record["acquired_at"], "没记什么时候拿的锁 —— 用户没法判断这是不是死锁"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_contending_for_the_lock_does_not_erase_who_holds_it(worktree) -> None:
    """抢锁失败的一方不能把持有者的身份抹掉。

    原来用 `open("w")` 打开 —— 来抢的人先截断文件。现场那个锁文件 0 字节，
    于是"是谁攥着"这个问题**因为有人来问而失去了答案**。
    """
    proc = _spawn_holder(_state_root(worktree))
    try:
        with pytest.raises(platform_runtime.ProjectBusyError) as caught:
            with platform_runtime._project_lock(_state_root(worktree)):
                pass
        message = str(caught.value)
        assert str(proc.pid) in message, f"错误信息没说是谁攥着：{message}"
        assert "since" in message, f"错误信息没说攥了多久：{message}"

        record = json.loads((_state_root(worktree) / ".chat.lock").read_text(encoding="utf-8"))
        assert record["pid"] == proc.pid, "抢锁失败把持有者记录冲掉了"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_worker_still_holding_the_lock_gets_reaped(worktree) -> None:
    """回收：判决落地。回收完锁必须真的能再拿到。"""
    from app.services.harness_sessions import reap_orphaned_session_worker

    proc = _spawn_holder(_state_root(worktree))
    try:
        reaped = reap_orphaned_session_worker("proj-1", "sess-1")
        assert reaped and str(proc.pid) in reaped, f"没回收：{reaped}"
        assert proc.poll() is not None or proc.wait(timeout=10) is not None

        # 判据不是"函数返回了一句话"，是**锁真的空出来了**。
        with platform_runtime._project_lock(_state_root(worktree)):
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_an_unrelated_process_is_never_killed(worktree) -> None:
    """pid 会被系统回收再分配 —— 拿一个陈旧的数字去杀无辜进程是最坏的结局。"""
    from app.services.harness_sessions import reap_orphaned_session_worker

    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock = _state_root(worktree) / ".chat.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": bystander.pid, "acquired_at": "x"}), encoding="utf-8")

        assert reap_orphaned_session_worker("proj-1", "sess-1") is None
        time.sleep(0.3)
        assert bystander.poll() is None, "杀了一个不是 worker 的进程"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


def test_a_dead_holder_is_not_reported_as_reaped(worktree) -> None:
    """进程早没了（flock 本来就自动释放）—— 别报告一个没发生的回收。"""
    from app.services.harness_sessions import reap_orphaned_session_worker

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    lock = _state_root(worktree) / ".chat.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": dead.pid, "acquired_at": "x"}), encoding="utf-8")

    assert reap_orphaned_session_worker("proj-1", "sess-1") is None


def test_the_reaper_never_kills_the_app_server_itself(worktree) -> None:
    """锁文件里写着自己的 pid（脏数据）也不能自杀。"""
    from app.services.harness_sessions import reap_orphaned_session_worker

    lock = _state_root(worktree) / ".chat.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    for pid in (os.getpid(), 1, 0, -5, "not-a-pid", None):
        lock.write_text(json.dumps({"pid": pid, "acquired_at": "x"}), encoding="utf-8")
        assert reap_orphaned_session_worker("proj-1", "sess-1") is None
    assert os.getpid() == os.getpid()  # 还活着


def test_no_lock_file_means_nothing_to_reap(worktree) -> None:
    """没有锁文件 / 没有 worktree —— 安静返回，别在启动路径上抛异常。"""
    from app.services.harness_sessions import reap_orphaned_session_worker

    assert reap_orphaned_session_worker("proj-1", "sess-1") is None
    assert reap_orphaned_session_worker("no-such-project", "no-such-session") is None


def test_the_holder_message_is_honest_when_the_record_is_missing() -> None:
    """老版本写的是裸 pid。读不懂就说"不知道是谁"，别假装这是个空锁。"""
    for junk in ("", "57214", "{}", "not json", '{"pid": null}'):
        message = platform_runtime._describe_lock_holder(junk)
        assert "already running" in message
        assert "unknown" in message, f"读不懂却没说不知道：{message!r}"


@pytest.mark.asyncio
async def test_the_sweep_reaps_while_it_relabels(db_session, worktree, monkeypatch) -> None:
    """接线：`mark_orphaned_harness_runs` 判完 stale 的同时把进程收掉。

    没有这一条，回收函数就是又一个"写了没人调"。
    """
    from app.models.execution import Run, RunStatus
    from app.services.harness_sessions import mark_orphaned_harness_runs

    proc = _spawn_holder(_state_root(worktree))
    try:
        from datetime import UTC, datetime, timedelta

        run = Run(
            id="run-orphan", tenant_id="t", workspace_id="w",
            project_id="proj-1", session_id="sess-1",
            status=RunStatus.RUNNING.value, summary={},
            # 过了出生宽限才算得上"无主"（2026-08-24 出生竞态不变量）
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db_session.add(run)
        await db_session.flush()

        # `reap_workers=True`：杀进程只在**启动**那一处成立 —— 那时注册表为空
        # 是构造上的事实，「不在注册表里」才等于「无主」。别处（登出/会话恢复/
        # assert_conversation_runtime_available）注册表里本就有活的东西，默认
        # 动手会误伤正在干活的子节点进程（2026-08-12 实测杀掉了 curator）。
        changed = await mark_orphaned_harness_runs(db_session, reap_workers=True)
        assert changed >= 1, "那条 run 没被判成无主"
        await db_session.refresh(run)
        # D11：不变量是"**真把进程杀了**"（下一条断言），不是"改了状态字段"。
        assert runtime_lost(run, has_live_binding=False) is True
        assert str(proc.pid) in str(run.summary.get("staleReapedWorker") or ""), (
            f"判决没落地，进程还在：summary={run.summary}"
        )
        assert proc.poll() is not None or proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
