"""长活的流不能把关机拖死 —— 而且这条不能靠"记得在循环里加一行"。

## 现场（2026-08-23，三次生产重启）

SIGTERM 之后后端不退：端口已释放、进程 state=SN、9 条库连接还攥着，每次只能
SIGKILL 收尾。三步判别实验定的因：

    A 无入站连接            → 1 秒干净退出
    B 挂一条 SSE 流（run 停在 waiting_permission）→ 20 秒还活着
    C 在 B 的状态下断掉客户端 → 1 秒后端自己退了

uvicorn 优雅关机**先关监听 socket、再等在途连接排空**，而 SSE 流按设计不会自己
结束（只在 run 到终态时 break，而 running / waiting_* 都不是终态）。

代价不止关不掉：lifespan 的 `finally`（停采集器、`detach_all()` 放开 harness
worker）**一次都没跑到过**。

## 这组测试钉两件事

1. 流真的会在关机时收手，并留下一个如实的 `reconnect` 帧（不是伪造 `end`）。
2. **新的流式端点绕不过去** —— 判据是扫盘，不是"我们改了这两处"。
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from app.services import lifecycle
from app.services.sse import RECONNECT_FRAME, until_shutdown


@pytest.fixture(autouse=True)
def _clean_lifecycle():
    lifecycle.reset_for_tests()
    yield
    lifecycle.reset_for_tests()


async def _never_ending():
    """就是那两个循环的形状：永远有下一块，永远不结束。"""
    try:
        while True:
            await asyncio.sleep(0.01)
            yield "event: execution\ndata: {}\n\n"
    finally:
        _never_ending.closed = True


@pytest.mark.asyncio
async def test_a_never_ending_stream_stops_when_shutdown_begins() -> None:
    lifecycle.install_shutdown_event()
    _never_ending.closed = False
    out: list[str] = []

    async def drain():
        async for chunk in until_shutdown(_never_ending()):
            out.append(chunk)

    task = asyncio.create_task(drain())
    await asyncio.sleep(0.05)
    assert not task.done(), "还没关机就自己停了 —— 那是另一个 bug"

    lifecycle.begin_shutdown()
    await asyncio.wait_for(task, timeout=3)

    assert out[-1] == RECONNECT_FRAME, "关机时必须留下一句交代，不能默默 EOF"
    assert _never_ending.closed, "源生成器的 finally 必须照跑（观察者注销/队列退订在里面）"


@pytest.mark.asyncio
async def test_the_last_frame_is_not_a_fake_terminal_end() -> None:
    """`end` 的语义是"这条 run 走完了"。拿它冒充重启 = 让前端把在跑的 run 记成终态。"""
    assert "event: reconnect" in RECONNECT_FRAME
    assert "event: end" not in RECONNECT_FRAME
    assert "server_shutdown" in RECONNECT_FRAME


@pytest.mark.asyncio
async def test_a_stream_opened_during_shutdown_runs_its_source_to_the_end() -> None:
    """开流时已经在关机中：**不接管**，让它自己跑完。

    这个机制的职责很窄 —— 让 uvicorn 排空阶段等着的那些连接能结束，而排空等的
    正是"信号到达时已经开着的连接"。把这条也接管过来会造成真实损失：chat 流的
    生成器体里有副作用（起 worker），赛跑瞬间判关机赢 = 那一步一次没跑过，于是
    重启打断的那一轮不再留下 `staleReason`，用户拿到「改改请求再试」。

    我第一版就是那么写的，被既有的
    `test_a_restart_leaves_the_run_recoverable_not_failed` 当场抓住。
    「交给 shutdown_detached_executions 收尾」也不行 —— 它是 cancel，不是等完。

    这一支的界在进程层（`--timeout-graceful-shutdown`），不在这里。
    """
    lifecycle.begin_shutdown()
    started = False

    async def worker_then_done():
        nonlocal started
        started = True                     # 生成器体的副作用（真实场景里是起 worker）
        yield "event: message\ndata: {}\n\n"
        yield 'event: message\ndata: {"type":"done"}\n\n'

    out = [chunk async for chunk in until_shutdown(worker_then_done())]
    assert started, "源生成器一次都没进去 —— 它的副作用（起 worker、记失败）也就没发生"
    assert len(out) == 2, "已在关机中时不该截断源自己的收尾序列"
    assert RECONNECT_FRAME not in out, "它是自己跑完的，没有被打断，不该多发一帧"


@pytest.mark.asyncio
async def test_a_normal_stream_still_ends_normally() -> None:
    """包装不能改变正常路径 —— 源自己结束就正常结束，不多发帧。"""
    lifecycle.install_shutdown_event()

    async def two_frames():
        yield "a"
        yield "b"

    assert [c async for c in until_shutdown(two_frames())] == ["a", "b"]


# ── 扫盘：新端点绕不过去 ─────────────────────────────────────────────────────

_LEGAL_SITE = Path("app/services/sse.py")


def _streaming_response_constructions() -> list[tuple[Path, int]]:
    hits: list[tuple[Path, int]] = []
    for path in sorted(Path("app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name == "StreamingResponse":
                hits.append((path, node.lineno))
    return hits


def test_streaming_responses_are_only_built_in_one_place() -> None:
    """判据是**扫盘**：合法的那条路命名出来，其余一律违规。

    只在那两个已知循环里各加一行 `if is_shutting_down(): break` 是名单式修法 ——
    第三个流式端点写进来照样把关机卡死，而且不报错。这条让"绕过去"变成红灯。
    """
    outside = [(p, n) for p, n in _streaming_response_constructions() if p != _LEGAL_SITE]
    assert not outside, (
        "这些地方直接构造了 StreamingResponse：\n"
        + "\n".join(f"  {p}:{n}" for p, n in outside)
        + f"\n流式响应必须走 {_LEGAL_SITE}::sse_response —— 它替所有流接上关机。"
        "\n直接构造 = 这条流会把 uvicorn 的优雅关机拖死，而且没有任何报错。"
    )


def test_the_guard_is_not_vacuous() -> None:
    """扫不到任何构造点 = 判据写错了地方，那比没写更糟（永远绿）。"""
    assert _streaming_response_constructions(), "一个 StreamingResponse 都没扫到？判据盯错地方了"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── 兜底那一条也得扫盘，不能靠"我们改了这三个脚本" ──────────────────────────

def test_every_uvicorn_launch_bounds_its_graceful_shutdown() -> None:
    """新起一个启动脚本却不带上限 → 红。

    这条兜底防的是**将来**某条长活循环绕过 `sse_response`：排空阶段的上限由我们
    定，而不是由某条连接肯不肯结束来定。但"我们改了那三个脚本"本身就是名单式
    修法 —— 第四个脚本写进来照样漏。所以判据同样是扫盘。

    ⚠️ 已知盲区：本机部署实际用的 `run-local.sh` **不在版本控制里**（它带着
    一个 API key），扫不到。那个影子副本得手工保持同步 —— 这条限制写在这里，
    好过假装它不存在（[[部署脚本影子副本]]）。
    """
    from tests.repository_sources import source_files

    offenders: list[str] = []
    for relative, text in source_files("*.sh"):
        for lineno, line in enumerate(text.splitlines(), 1):
            if "uvicorn app.main:app" not in line or line.lstrip().startswith("#"):
                continue
            if "--timeout-graceful-shutdown" not in line:
                offenders.append(f"{relative}:{lineno}")
    assert not offenders, (
        "这些地方起 uvicorn 却没有给优雅关机上限：\n  " + "\n  ".join(offenders)
        + "\n没有上限时，一条不肯结束的连接就能让进程永远退不掉"
        "（2026-08-23 实测：三次生产重启只能 SIGKILL 收尾）。"
    )


def test_the_launch_scan_is_not_vacuous() -> None:
    from tests.repository_sources import source_files

    found = [
        relative for relative, text in source_files("*.sh")
        if "uvicorn app.main:app" in text
    ]
    assert found, "一个 uvicorn 启动点都没扫到？判据盯错地方了"
