"""孤儿扫描不能把**正在干活**的子节点进程杀掉。

## 现场（2026-08-12，Mac 上跑着的无人值守实验）

    01:28:58 | run_d11394c2…/_orchestrator->_curator@d1 | pid 26592 terminated

curator 正在干活，被 SIGTERM 掉。用户在 UI 上看到的是
「The research runtime stopped unexpectedly」。

## 两个 bug 叠在一起，都是前一晚我自己引入的

**① 子节点 run 永远通不过"有没有 binding"这道判据。**

子节点 run 从 2026-08-12 起有了自己的 Run 行（`<父>/<子>`），而注册表里只登记
**顶层**那一条（一个 harness 子进程 = 一条 binding）。于是

    if binding and binding.run_id == run.id: continue

对每一个子节点都不成立 → 全被判成孤儿。

子节点的死活取决于**父 run**：父还有进程，子就还活着。「顶层 run」和
「子节点 run」是两种东西，判活的问题不一样 —— 又一次"两件事一条规则"。

**② 杀进程被挂在了一个 4 个调用点的记账函数上。**

`mark_orphaned_harness_runs` 判"无主"的依据是"注册表里没有它"。这个推理
**只在启动时成立**（那时注册表为空是构造上的事实）。另外三个调用点
（登出 / 会话恢复 / `assert_conversation_runtime_available`）注册表里本就有
活的东西。而 `assert_conversation_runtime_available` 调它时**不带任何过滤、
扫全表** —— 于是每次有人往一个 waiting_human 的会话发消息，全表的子节点
进程就被清一遍。

记账错了还能改回来，杀进程改不回来。这两件事该有不同的门槛。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.execution import Run, RunStatus, SessionProjection


async def _seed(db, *, parent_id: str, child_id: str) -> None:
    db.add(SessionProjection(
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p-sweep", session_id="s-sweep", initiating_user_id="u",
    ))
    from datetime import UTC, datetime, timedelta

    for run_id, parent in ((parent_id, None), (child_id, parent_id)):
        db.add(Run(
            id=run_id, tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id="p-sweep", session_id="s-sweep", parent_run_id=parent,
            status=RunStatus.RUNNING.value, summary={},
            # 过了出生宽限才算得上"无主"（2026-08-24 出生竞态不变量）
            created_at=datetime.now(UTC) - timedelta(hours=1),
        ))
    await db.flush()


@pytest.mark.asyncio
async def test_a_child_run_is_alive_while_its_parent_is(db_session, monkeypatch) -> None:
    """父 run 还有活进程 → 它的子节点不是孤儿。"""
    from app.services import harness_sessions
    from app.services.harness_sessions import AppRunBinding, mark_orphaned_harness_runs

    await _seed(db_session, parent_id="run_parent", child_id="run_parent/_curator@d1")
    monkeypatch.setattr(
        harness_sessions.harness_session_manager, "live_binding",
        lambda pid, sid: AppRunBinding("u", "s-sweep", "run_parent", "s-sweep"),
    )

    changed = await mark_orphaned_harness_runs(db_session)
    assert changed == 0, "父进程还活着，却把它和子节点判成了孤儿"

    rows = {r.id: r.status for r in (await db_session.execute(
        select(Run).where(Run.session_id == "s-sweep")
    )).scalars()}
    assert rows["run_parent/_curator@d1"] == RunStatus.RUNNING.value, (
        "正在干活的子节点被判成 stale —— 接着回收器就会 SIGTERM 它"
    )


@pytest.mark.asyncio
async def test_a_child_whose_parent_is_gone_is_still_reaped(db_session, monkeypatch) -> None:
    """父进程真没了 → 子节点照旧要回收。放宽不能变成放过。"""
    from app.services import harness_sessions
    from app.services.harness_sessions import mark_orphaned_harness_runs

    await _seed(db_session, parent_id="run_dead", child_id="run_dead/_curator@d1")
    monkeypatch.setattr(
        harness_sessions.harness_session_manager, "live_binding", lambda pid, sid: None
    )

    changed = await mark_orphaned_harness_runs(db_session)
    assert changed == 2, f"父没了，父子都该回收，实际改了 {changed} 条"


@pytest.mark.asyncio
async def test_killing_processes_is_off_unless_asked(db_session, monkeypatch) -> None:
    """默认只记账，不杀进程。

    这个函数有 4 个调用点，只有启动那一处具备"注册表为空 ⇒ 全都无主"的性质。
    默认动手 = 另外三处每次调用都在清全表的子进程。
    """
    from app.services import harness_sessions
    from app.services.harness_sessions import mark_orphaned_harness_runs

    killed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        harness_sessions.harness_session_manager, "live_binding", lambda pid, sid: None
    )
    monkeypatch.setattr(
        harness_sessions, "reap_orphaned_session_worker",
        lambda pid, sid: killed.append((pid, sid)) or "pid 1 terminated",
    )

    await _seed(db_session, parent_id="run_a", child_id="run_a/_curator@d1")
    changed = await mark_orphaned_harness_runs(db_session)
    assert changed == 2, "这一批本来就该被判成孤儿（父进程不在）"
    assert killed == [], f"默认就动手杀进程了：{killed}"

    # 上一批已经落终态、扫不到了 —— 换一批新的来验"显式要求时真的会动手"。
    from datetime import UTC, datetime, timedelta

    db_session.add(Run(
        id="run_b", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p-sweep", session_id="s-sweep", parent_run_id=None,
        status=RunStatus.RUNNING.value, summary={},
        created_at=datetime.now(UTC) - timedelta(hours=1),
    ))
    await db_session.flush()
    await mark_orphaned_harness_runs(db_session, reap_workers=True)
    assert killed, "显式要求回收时反而没杀 —— 那启动清理就白做了"


def test_only_startup_asks_for_the_kill() -> None:
    """扫盘：只有启动那一处能传 `reap_workers=True`。

    列名单会漏掉将来新加的调用点；扫盘不会。判据是"谁在要求动手"，
    而不是"有哪几个调用点"。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    askers: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "mark_orphaned_harness_runs":
                continue
            for kw in node.keywords:
                if kw.arg == "reap_workers" and getattr(kw.value, "value", False) is True:
                    askers.append(str(path.relative_to(root)))

    assert askers == ["main.py"], (
        f"这些地方也在要求杀进程，而只有启动时那个推理才成立：{askers}"
    )
