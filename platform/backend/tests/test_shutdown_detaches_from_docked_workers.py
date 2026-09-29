"""后端收摊放手不杀 worker：停靠中的、跑着的，都活过 lifespan 的收尾段。

## 现场（2026-09-03 14:01，node20 部署 0c9e557）

部署脚本收尾报「部署前存活 worker 1 / 部署后 0」并 exit 1。凶手是后端自己：
session b4ebcffe 的 chat run 13:59:46 completed 后 worker 停靠
（unattended_loop_stopped reason=followup_declined），events.jsonl 最后一条
`{"type":"terminated",…,"reason":"terminate"}` 在 06:01:17.664Z，与旧后端
backend.log 的「Shutting down Research Platform 14:01:17,817 +0800」同一秒。
路径：main.lifespan 的 finally → harness_session_manager.terminate_all() →
每个会话发 op=terminate。

那一行是 8-03 写的：worker 还是 stdin 子进程的年代，后端一死管道 EOF，worker
本来就跟着走，terminate 只是让它体面一点。P0（8-18~8-23）让 worker 自成进程
组、命令面走 socket、事件落盘、新后端按注册表接回来，部署脚本据此承诺"不动
worker" —— 而收尾段这一行没人回头看。承诺只在零停靠 worker 时成立。

## 这里验的是真入口

不 mock manager：真 `lifespan`（startup 走一遍、shutdown 走一遍）、真
`harness_session_manager`、真 worker 进程（真 socket、真注册表行、真
events.jsonl）、真 asyncio 子进程 transport（那正是 Linux 上会替我们把孩子
杀掉的东西，见 `_SpawnedWorker.release`）。会话对象是手工装配的（不走
`_new_session`，那要 LLM 凭据与 init），但它握着的每一样东西都是真的。
"""

from __future__ import annotations

import ast
import asyncio
import gc
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from app.config import settings
from app.services import harness_sessions, lifecycle
from app.services.harness_runtime import _drain_stderr
from app.services.harness_sessions import (
    AppRunBinding,
    _ChildOwnedByTheOS,  # noqa: F401 - 扫盘闸用它定位边界
    _spawn_child_owned_by_the_os,
    _connect_control_address,
    _process_is_a_runtime_worker,
    _ProjectHarnessSession,
    _SpawnedWorker,
    harness_session_manager,
)
from tests._live_worker import (
    HARNESS_ROOT,
    declare_activity,
    event_types,
    kill_if_alive,
    pid_alive,
    runtime_layout,
    spawn_worker,
    spawn_worker_async,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT, SESSION = "proj-detach", "sess-detach"


async def _noop(_event: dict) -> None:
    return None


@pytest.fixture
def short_root():
    # unix socket 路径有长度上限 —— 与 reattach 那组测试一样落在 /tmp 短路径下。
    root = Path(tempfile.mkdtemp(prefix="hsd-", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def quiet_startup(monkeypatch, db_engine):
    """让 lifespan 的**启动**段在测试里走得通；收尾段一个字不碰。

    startup 里与本题无关、又要外部世界的几步：harness 运行时探针与沙箱探针
    （要 docker）、资讯采集、文献索引采集、沙箱容器对账。关掉 / 换成空操作；
    schema 闸、数据根闸、孤儿对账、交付对账、关机旗都照真的跑（sqlite 测试库
    由 `db_engine` 接到模块级 session factory）。
    """
    monkeypatch.setattr(settings, "local_demo_mode", True)  # 默认 secret_key 只在 demo 模式放行
    monkeypatch.setattr(settings, "harness_bridge_enabled", False)
    monkeypatch.setattr(settings, "feed_collector_enabled", False)
    monkeypatch.setattr(settings, "literature_harvester_enabled", False)

    lifecycle.reset_for_tests()
    yield
    lifecycle.reset_for_tests()


@pytest.fixture
def registry(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(harness_session_manager, "_sessions", store)
    return store


@pytest.fixture
def worker_paths(short_root, monkeypatch):
    """App Server 侧的路径推导（worktree 根 / 注册表行 / socket 地址）都指到这里。"""
    from app.services.harness_contract import _harness_root, worker_addressing

    worktree_root = short_root / "wt"
    state_root = runtime_layout(worktree_root, PROJECT, SESSION)
    state_root.mkdir(parents=True)
    monkeypatch.setattr(settings, "project_worktree_root", str(worktree_root))
    monkeypatch.setattr(settings, "harness_root", str(HARNESS_ROOT))
    _harness_root.cache_clear()
    sock = worker_addressing().control_socket_path(short_root, PROJECT, SESSION)
    yield state_root, sock
    _harness_root.cache_clear()


@pytest_asyncio.fixture
async def docked_worker(worker_paths):
    """一个像 b4ebcffe 那样停靠着的 worker：跑完一轮、空闲、自报 idle。

    与 `_new_session` 同一种 spawn（asyncio、三根 PIPE、start_new_session）、
    同一种收发面（socket）、同一种句柄（`_SpawnedWorker`）。
    """
    state_root, sock = worker_paths
    proc = await spawn_worker_async(state_root, sock, spawn_token="tok-docked")
    declare_activity(state_root, pid=proc.pid, spawn_token="tok-docked", state="idle")
    # 带上 token —— 夹具的 worker 现在和线上一样做身份握手（`_control_request_source`
    # 从 env 拿 token）。不带就会被拒，而那正是这道握手存在的意义。
    channel = await _connect_control_address(str(sock), spawn_token="tok-docked")
    session = _ProjectHarnessSession(
        project_id=PROJECT,
        session_id=SESSION,
        owner_user_id="u1",
        backend_id="backend-1",
        backend_fingerprint="fp",
        platform_context_hash=None,
        process=proc,
        stderr_task=asyncio.create_task(_drain_stderr(proc.stderr)),
        provider_secrets=(),
        spawn_token="tok-docked",
        channel=channel,
        worker=_SpawnedWorker(proc),
    )
    session.binding = AppRunBinding("u1", SESSION, "run-docked", SESSION)
    try:
        yield session, proc, state_root
    finally:
        kill_if_alive(proc.pid)


# ── 真入口：lifespan ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_docked_worker_survives_backend_shutdown(quiet_startup, registry, docked_worker):
    """现场那一种：停靠中的 worker，后端收摊。它必须还活着、没收到 terminate，
    而且下一个后端接得回来。"""
    from app.main import app, lifespan

    session, proc, state_root = docked_worker
    key = harness_session_manager._key(PROJECT, SESSION)

    async with lifespan(app):
        registry[key] = session  # 与一次真 spawn 之后一样：注册表里有它
        assert harness_session_manager.active_count == 1

    # 收尾段跑完了：本进程的记账清空，worker 一根毫毛没少。
    assert registry == {}
    assert harness_session_manager.active_count == 0
    assert _process_is_a_runtime_worker(proc.pid), "后端收摊把停靠中的 worker 杀了"
    assert "terminated" not in event_types(state_root), "收尾段给 worker 发了 terminate"

    # 下一个后端接得回来：socket 回到了 accept 上，注册表行还在。
    assert await harness_session_manager.adopt_live_worker(
        PROJECT, SESSION, owner_user_id="u1"
    ), "放开之后接不回来 —— 那放开就只是换了一种丢法"
    assert harness_session_manager.active_count == 1


@pytest.mark.asyncio
async def test_a_busy_worker_survives_backend_shutdown(quiet_startup, registry, docked_worker):
    """一次会话面 RPC 在飞（本后端视角：`claim()` 攥着操作锁）时收摊。

    老写法 `terminate()` 等锁 5 秒等不到就 `_kill()`：socket 关掉、再等 5 秒、
    SIGKILL —— 正跑着几小时实验的 worker 从前就是这么死的。
    """
    from app.main import app, lifespan

    session, proc, state_root = docked_worker
    declare_activity(
        state_root, pid=proc.pid, spawn_token="tok-docked", state="working",
        app_binding={"user_id": "u1", "conversation_id": SESSION,
                     "run_id": "run-docked", "session_id": SESSION},
        detail={"step": "experiment"},
    )
    key = harness_session_manager._key(PROJECT, SESSION)

    started = asyncio.get_running_loop().time()
    async with session.claim():
        async with lifespan(app):
            registry[key] = session
    elapsed = asyncio.get_running_loop().time() - started

    assert _process_is_a_runtime_worker(proc.pid), "后端收摊把正在干活的 worker 杀了"
    assert "terminated" not in event_types(state_root)
    assert elapsed < 4, f"收摊等了 {elapsed:.1f}s —— 在等一个不该等的锁 / 进程"


@pytest.mark.asyncio
async def test_detaching_an_old_stdio_worker_does_not_wait_for_it():
    """老 stdio worker（`_PipeChannel`）：关掉 stdin 就是 EOF，它按自己的节奏
    跑完队列退场（P0-3）。放手这一步既不等它、也不杀它。"""
    proc = await _spawn_child_owned_by_the_os(
        [sys.executable, "-c", "import sys, time; sys.stdin.read(); time.sleep(5)"],
        cwd=str(BACKEND_DIR), env=dict(os.environ), limit=64 * 1024,
        start_new_session=True,
    )
    session = _ProjectHarnessSession(
        project_id="p", session_id="s", owner_user_id="u", backend_id="b",
        backend_fingerprint="fp", platform_context_hash=None,
        process=proc, stderr_task=asyncio.create_task(_drain_stderr(proc.stderr)),
        provider_secrets=(),
    )
    try:
        started = asyncio.get_running_loop().time()
        await session.detach()
        assert asyncio.get_running_loop().time() - started < 1.0, "放手在等进程退出"
        await asyncio.sleep(0.3)
        assert pid_alive(proc.pid), "放手把老 stdio worker 杀了（它该收到 EOF 后自己退场）"
        assert session.alive is False and session.process is None
    finally:
        kill_if_alive(proc.pid)


# ── 放手：孩子归操作系统，不归事件循环 ────────────────────────────────────
#
# 2026-09-16 现场：`release()` 去改 asyncio subprocess transport 的私有属性
# `_closed`，好让 transport 收摊时别 kill 孩子。生产跑的是 **uvloop**，它的
# `UVProcessTransport` 没有这个属性：
#
#     AttributeError: 'uvloop.loop.UVProcessTransport' object has no attribute '_closed'
#     ERROR:    Application shutdown failed. Exiting.
#
# backend.log 里这一对出现了 9 次 —— 每一次后端关机。而 `detach_all` 是顺序
# 循环，第一个抛出就把后面所有 worker 的 detach 一起带走：「部署放手不杀
# worker」在生产上从来没生效过。
#
# 而钉住 `release()` 的测试**只跑 asyncio**，所以它一直绿。判据依赖运行环境 =
# 判据没覆盖生产。所以下面两条按事件循环参数化，uvloop 必须在列。

_LOOP_FACTORIES: dict[str, object] = {"asyncio": asyncio.new_event_loop}
try:  # 生产装的是 uvloop；本机没装也不该让这条测试消失（会看不出覆盖缺口）
    import uvloop as _uvloop

    _LOOP_FACTORIES["uvloop"] = _uvloop.new_event_loop
except ImportError:  # pragma: no cover
    pass


def test_the_loop_the_backend_actually_runs_on_is_covered():
    """生产跑 uvloop。判据不覆盖它，等于不覆盖生产。"""
    assert "uvloop" in _LOOP_FACTORIES, (
        "uvloop 没装 → 下面两条只验了 asyncio，而生产跑的是 uvloop。"
        "这正是 2026-09-16 那次故障的形状：绿色不指向被测的事。"
    )


@pytest.mark.asyncio
async def test_the_fixture_hands_release_something_it_can_actually_release(worker_paths):
    """夹具造出来的 worker 必须**放得开** —— 这是它和生产同一条 spawn 的可观测后果。

    `release()` 调的是 `close_pipes()`，只有 `_ChildOwnedByTheOS` 有它。夹具一旦
    退回 `asyncio.create_subprocess_exec`，这里当场 `AttributeError`；而在
    `detach` 那条路上它会被收尾层吞成一行日志，孩子死不死要看 asyncio 的 transport
    什么时候被 GC —— Linux 上会、macOS 上不会，于是变成"CI 间歇红、本机永远绿"。

    判据落在**放手之后孩子还在**上，不在"夹具里写了哪个函数名"上。
    """
    state_root, sock = worker_paths
    proc = await spawn_worker_async(state_root, sock, spawn_token="tok-releasable")
    try:
        _SpawnedWorker(proc).release()      # 不许抛
        await asyncio.sleep(0.3)
        assert proc.returncode is None, "放手把孩子带走了 —— 那不是放手"
        assert _process_is_a_runtime_worker(proc.pid), "放手之后它不再是一个活着的 worker"
    finally:
        kill_if_alive(proc.pid)


@pytest.mark.parametrize("loop_name", sorted(_LOOP_FACTORIES))
def test_a_released_child_survives_its_event_loop(loop_name):
    """放手之后，事件循环收摊带不走这个孩子 —— 两种循环都必须成立。"""
    loop = _LOOP_FACTORIES[loop_name]()
    asyncio.set_event_loop(loop)
    try:
        child = loop.run_until_complete(_spawn_child_owned_by_the_os(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(BACKEND_DIR), env=dict(os.environ), limit=64 * 1024,
            start_new_session=True,
        ))
        pid = child.pid
        _SpawnedWorker(child).release()
    finally:
        loop.run_until_complete(asyncio.sleep(0.05))
        loop.close()
    try:
        del child
        gc.collect()
        time.sleep(0.4)
        assert pid_alive(pid), f"{loop_name}: 事件循环收摊把已放手的孩子带走了"
    finally:
        kill_if_alive(pid)


@pytest.mark.asyncio
async def test_the_old_ownership_model_really_did_kill_the_child():
    """对照组：证明这条测试钉的危险是真的 —— 交给事件循环拥有的孩子，
    transport 一 close 就被 kill。我们现在不走那条路了（`_ChildOwnedByTheOS`）。"""
    victim = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    victim._transport.close()          # `__del__` 做的就是这一步
    assert await asyncio.wait_for(victim.wait(), timeout=10) is not None
    assert not pid_alive(victim.pid), "对照组没死 —— 这条测试钉的危险不存在了？"


def test_release_touches_no_private_event_loop_state():
    """扫盘：放手这条路上不许再出现事件循环的私有属性。

    名单会漏，形状不会：`_closed` 当初就是"全后端唯一一处"，而唯一一处也足够
    让生产坏 9 次。
    """
    source = (BACKEND_DIR / "app/services/harness_sessions.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # **每一个** release —— 基类那个是空实现，只查它等于什么都没查
    # （第一版就是这么写的，变异当场从它底下溜过去了）。
    releases = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "release"
    ]
    assert len(releases) >= 2, f"只找到 {len(releases)} 个 release —— 正则/AST 失效了"
    private = [
        f"{node.attr}（{fn.lineno} 行那个 release）"
        for fn in releases
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute) and node.attr.startswith("_")
    ]
    assert private == [], f"release() 又碰私有属性了：{private}"
    assert "_transport" not in source.split("class _ChildOwnedByTheOS")[0], (
        "spawn/detach 路径上还残留着对 asyncio transport 的依赖"
    )


@pytest.mark.parametrize("loop_name", sorted(_LOOP_FACTORIES))
def test_a_released_worker_outlives_the_backend_process(loop_name):
    """端到端：一个"后端"进程 spawn 孩子、release、正常退出。孩子必须还在。

    这条是真退出的对账 —— in-process 那条测不到"解释器收摊"这一段。
    """
    script = textwrap.dedent(
        f"""
        import asyncio, os, sys
        sys.path[:0] = [{str(BACKEND_DIR)!r}, {str(HARNESS_ROOT)!r}]
        {"import uvloop; asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())"
         if loop_name == "uvloop" else ""}
        from app.services.harness_sessions import (
            _SpawnedWorker, _spawn_child_owned_by_the_os,
        )

        async def main():
            child = await _spawn_child_owned_by_the_os(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd={str(BACKEND_DIR)!r}, env=dict(os.environ), limit=64 * 1024,
                start_new_session=True,
            )
            print(child.pid, flush=True)
            _SpawnedWorker(child).release()

        asyncio.run(main())
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND_DIR,
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    pid = int(done.stdout.split()[0])
    try:
        time.sleep(0.5)
        assert pid_alive(pid), f"{loop_name}: 后端进程退出把已放手的孩子带走了"
    finally:
        kill_if_alive(pid)


# ── 接回来：真入口 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_adopts_a_live_worker_through_the_real_entry(
    db_session, registry, worker_paths
):
    """P0-4 的先接后判，从真入口走：`mark_orphaned_harness_runs(reap_workers=True)`。

    2026-09-03 之前 `adopt_live_worker` 锁在一个不存在的属性上（`self._lock`），
    AttributeError 被调用方的 `suppress(Exception)` 吞掉 → 一次都没接成过 →
    这条 run 被判无主、worker 被 SIGTERM。CI 全绿，因为没有一条测试走到过
    这个方法。
    """
    from app.models.execution import Run, RunStatus, SessionProjection
    from app.services.harness_sessions import mark_orphaned_harness_runs

    state_root, sock = worker_paths
    proc = spawn_worker(state_root, sock, spawn_token="tok-adopt")
    try:
        declare_activity(
            state_root, pid=proc.pid, spawn_token="tok-adopt", state="working",
            app_binding={"user_id": "u1", "conversation_id": SESSION,
                         "run_id": "run-live", "session_id": SESSION},
            detail={"step": "experiment"},
        )
        db_session.add(SessionProjection(
            tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id=PROJECT, session_id=SESSION, initiating_user_id="u1",
        ))
        db_session.add(Run(
            id="run-live", tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id=PROJECT, session_id=SESSION, status=RunStatus.RUNNING.value,
            summary={},
            # 过了出生宽限才可能被判"无主"—— 这里要的正是"本来会被判、接回来就不判"。
            created_at=datetime.now(UTC) - timedelta(hours=1),
        ))
        await db_session.flush()

        changed = await mark_orphaned_harness_runs(db_session, reap_workers=True)

        assert changed == 0, "worker 明明活着、接得回来，run 却被判成了无主"
        binding = harness_session_manager.live_binding(PROJECT, SESSION)
        assert binding is not None and binding.run_id == "run-live", "接回来了但绑定没跟着回来"
        assert _process_is_a_runtime_worker(proc.pid), "接回来的下一秒被自己人杀了"
    finally:
        kill_if_alive(proc.pid)


@pytest.mark.asyncio
async def test_spawning_over_a_live_worker_adopts_it_first(registry, worker_paths, monkeypatch):
    """上一代后端放走的空闲 worker 还攥着 session 的 flock。下一条消息要 spawn 时，
    先接回来、体面 terminate（锁释放）、再 spawn —— 而不是让新 worker 撞锁以
    project_busy 退场（那句"会话被占用"里占用它的正是我们自己放走的进程）。
    """
    from app.models.model_backend import ModelBackendConfig
    from app.models.user import User
    from app.services.model_backends import encrypt_api_key

    state_root, sock = worker_paths
    proc = spawn_worker(state_root, sock, spawn_token="tok-idle")
    old_worker_alive_at_spawn: list[bool] = []

    async def fake_new_session(**_kwargs):
        old_worker_alive_at_spawn.append(_process_is_a_runtime_worker(proc.pid))
        return object()

    monkeypatch.setattr(harness_session_manager, "_new_session", fake_new_session)
    try:
        declare_activity(state_root, pid=proc.pid, spawn_token="tok-idle", state="idle")
        user = User(
            id="u1", email="u1@example.test", hashed_password="unused",
            institution_id="institution-1", institution_name="Institution",
        )
        backend = ModelBackendConfig(
            id="backend-1", scope_kind="personal", scope_id="u1",
            provider="openai_compatible", display_name="Bridge", model="fake-model",
            base_url="http://provider.invalid/v1", credential_source="encrypted",
            encrypted_api_key=encrypt_api_key("secret"), created_by_user_id="u1",
        )
        await harness_session_manager._get_or_create(
            user=user, project_id=PROJECT, session_id=SESSION,
            role_bindings={"reasoning": backend},
            platform_context_snapshot=None,
            on_progress=_noop, on_protocol_event=_noop,
        )
        assert old_worker_alive_at_spawn == [False], (
            "spawn 时老 worker 还攥着锁 —— 没先接回来，或者没等它退场"
        )
        assert "terminated" in event_types(state_root), (
            "老 worker 不是体面退场的（该是 op=terminate，不是 SIGKILL）"
        )
    finally:
        kill_if_alive(proc.pid)


# ── 扫盘：关机路径只许放手 ─────────────────────────────────────────────────


def _calls_named(root: pathlib.Path, name: str) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == name:
                    hits.append(str(path.relative_to(root)))
    return sorted(set(hits))


def test_the_shutdown_path_detaches_and_nothing_in_app_terminates_everything() -> None:
    """扫盘：`main.py` 收尾段调 `detach_all`；`terminate_all` 在 app/ 里零调用方。

    列名单会漏掉将来新加的调用点；扫盘不会。判据是"谁在把所有会话结束"，
    而结束所有会话在生产里没有任何一个正当理由 —— 关机不是。
    """
    root = BACKEND_DIR / "app"
    assert _calls_named(root, "detach_all") == ["main.py"], "关机路径没有放手（detach_all）"
    assert _calls_named(root, "terminate_all") == [], (
        "有人在生产代码里把所有会话结束了 —— 关机该放手，登出该按用户终止"
    )
    assert os.path.isfile(root / "main.py")  # 扫的根没扫错（不是空目录恒真）
