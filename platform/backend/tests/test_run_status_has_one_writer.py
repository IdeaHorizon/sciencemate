"""`Run.status` 只能有一个写者（RFC 异步运行时 D11 的扫盘闸）。

RFC 原文：

    配**扫盘闸**（AST/grep 级测试断言写点唯一==1），不是名单 ——
    「护栏要扫盘，不要写名单」；单写者迁移不完全是旧病最可能复发的地方。

## 为什么是这条

`run.status` 是事实字段：这条 run 在干什么。投影器把 worker 说的话翻译进去，
那是转述事实。平台在别处直接写，写的是**判决**（"我猜它没主了"）——判决错了
没人知道，而下游已经按它行动过了。

迁移前有 5 个写点，4 个是平台的猜测。它们造成的事故：

- 一条正常推进的 run 在开跑 9 秒时被判死，UI 停止轮询五分钟（2026-08-21）；
- 一次部署把两条 run 判死，其中一条是在 `retrying` 上被盖的；
- 覆盖之后判据**再也算不出来** —— `stale_unknown` 不在
  REQUIRES_LIVE_RUNTIME_STATUSES 里，"它当时在跑还是在等人"永远问不回来。

## 2026-08-23：从"函数级白名单"收成"写点真的唯一"

这个文件原来放行三个**函数**（投影器、平台自身异常、活 pause 重建），理由都
成立。但白名单守不住 8-21 那条 run 的真正形状：它收到两次终态，中间还在推进
研究 —— 三个写者各自都"合法"，而**没有任何一处记得自己是第几个写的**，库里
只剩最后一个值，前一次盖错了连痕迹都没有。

所以三处都保留（理由没变），但它们改为经同一个漏斗
`run_status.project_run_status(run, status, source=…, evidence=…)`：

  · 写点**真的**只有一个 —— "还有谁在写"从此是确定答案，不是要 grep 全仓；
  · 每一次写留下**出处**（谁写的、依据什么），盖错了看得见；
  · 终态被后续活动推翻时留疤（`terminalContradicted`），而不是被覆盖掉。

原来的"函数级白名单"降级成 `run_status.SOURCES` 里的三个取值 —— 判据没变
（**有没有一个当下可观测的东西支撑这个写**），只是从测试文件挪进了产品代码，
于是它在运行时也成立。

## 两道闸，各防各的

  · **运行时闸**（`Run.status` 的 set 监听器，在 models 里、紧挨着那一列）：
    第二个写者当场撞墙，生产里也成立；
  · **扫盘闸**（本文件最后一条）：把"写点唯一"这件事本身钉住，是扫盘不是名单。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.models.execution import Run, RunStatus, RunStatusWriteError
from app.services.run_status import (
    CONTRADICTED_KEY,
    MAX_STATUS_LOG,
    SOURCES,
    STATUS_LOG_KEY,
    project_run_status,
)

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _run(status: str = "queued") -> Run:
    return Run(
        id="run-1", tenant_id="t", workspace_id="w",
        project_id="p", session_id="s", status=status,
    )


def _status_writes(path: pathlib.Path) -> list[tuple[str, int]]:
    """`<something with "run" in its name>.status = ...` 的 (所在函数, 行号)。

    按名字里有没有 `run` 筛，会连 `projected_run` / `parent_run` 一起抓到 ——
    **宁可多抓**：多抓的会在人眼前出现一次，漏抓的会变成第二个悄悄的写者。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(scope):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "status"
                    and "run" in ast.unparse(target.value).lower()
                ):
                    found.append((scope.name, node.lineno))
    return found


# ── 扫盘闸 ──────────────────────────────────────────────────────────────────

def test_exactly_one_place_in_the_whole_backend_writes_run_status() -> None:
    writers: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(APP.parent))
        writers += [f"{rel}:{line} 在 {func}()" for func, line in _status_writes(path)]
    assert len(writers) == 1 and "run_status.py" in writers[0], (
        "Run.status 的写点不再唯一（RFC D11）：\n  " + "\n  ".join(writers)
        + "\n\n多出来的那一处不会留下出处 —— 而没有出处的状态变化正是 D11 要消灭的。"
        "\n经 run_status.project_run_status 写，并在 SOURCES 里登记它凭什么是转述而不是判决。"
    )


def test_the_gate_can_actually_see_the_writes() -> None:
    """闸自己要能看见东西 —— 扫不到任何写点时上面那条会空转通过。"""
    assert _status_writes(APP / "services/run_status.py"), (
        "在漏斗里都扫不到 run.status 的写 —— AST 判据失效了"
    )


def test_every_writer_is_still_named() -> None:
    """白名单没有消失，它搬进了产品代码。

    判据一个字没改：**有没有一个当下可观测的东西支撑这个写**。
    有 → 转述，允许；没有（只有"注册表里查不到"这种推断）→ 判决，禁止。
    """
    assert set(SOURCES) == {
        "projector",                  # 转述 worker 的事件
        "live_pause_reconciliation",  # 从当下真实存在的活 pause 重建
        "transport_failure",          # 平台亲眼所见的自身失败
        # 转述"有人开了新的一轮"这个可观测事实：会话面 RPC 串行，新的顶层 run
        # 一开跑，上一轮就**已经**不是当前一轮了。不写终态的代价是它永远挂在
        # waiting_human 上，把一个答过的问题反复递给人（2026-08-23 e46448f0）。
        "superseded_by_next_turn",
    }


# ── 运行时闸 ────────────────────────────────────────────────────────────────

def test_creating_a_run_may_set_its_initial_status() -> None:
    """定初值不是改状态 —— 闸不许把创建也挡了。"""
    assert _run("queued").status == "queued"


def test_a_direct_write_is_refused_at_runtime() -> None:
    """第二个写者当场撞墙。

    扫盘闸只在有人跑测试时说话；这一条在生产里也成立 —— 而要防的缺陷恰好
    是"在生产里悄悄发生、库里只剩最后一个值"。
    """
    run = _run()
    with pytest.raises(RunStatusWriteError, match="project_run_status"):
        run.status = "running"
    assert run.status == "queued", "被拒的写不许留下半个效果"


# ── 出处与疤痕 ──────────────────────────────────────────────────────────────

def test_every_write_says_who_wrote_it_and_why() -> None:
    run = _run()
    project_run_status(
        run, RunStatus.RUNNING,
        source="projector", evidence={"eventId": "ev-1", "kind": "run.started"},
    )
    entry = run.summary[STATUS_LOG_KEY][-1]
    assert (entry["from"], entry["to"]) == ("queued", "running")
    assert entry["source"] == "projector"
    assert entry["evidence"]["eventId"] == "ev-1"


def test_an_unregistered_source_is_refused() -> None:
    """`SOURCES` 是"还有谁在写"的清单。绕过登记就等于清单没用。"""
    with pytest.raises(RunStatusWriteError, match="未登记"):
        project_run_status(_run(), RunStatus.RUNNING, source="somewhere")


def test_a_terminal_contradicted_by_later_work_leaves_a_scar() -> None:
    """8-21 那条 run 的形状：盖了终态，然后它接着干活。

    没有任何合法路径能让一条真结束的 run 回到运行中 —— 所以"终态之后又来了
    非终态"就是"前一个终态盖早了"的直接证据。这件事必须留在 run 自己的记录
    里，否则下一次还得靠人去翻原始事件流才发现。
    """
    run = _run()
    project_run_status(run, RunStatus.RUNNING, source="projector", evidence={})
    project_run_status(
        run, RunStatus.INCOMPLETE, source="projector", evidence={"eventId": "ev-early"}
    )
    assert CONTRADICTED_KEY not in run.summary, "终态本身不是缺陷"

    project_run_status(
        run, RunStatus.RUNNING, source="projector", evidence={"eventId": "ev-later"}
    )
    scar = run.summary[CONTRADICTED_KEY][-1]
    assert scar["terminal"] == "incomplete"
    assert scar["contradictedBy"] == "running"
    assert scar["evidence"]["eventId"] == "ev-later"


def test_a_terminal_after_a_terminal_is_not_a_scar() -> None:
    """`incomplete → completed` 是合法的收尾修正，不是"它没结束"。

    判据要准：把所有二次终态都当缺陷，真正的那一类就淹没在噪音里了。
    """
    run = _run()
    project_run_status(run, RunStatus.INCOMPLETE, source="projector", evidence={})
    project_run_status(run, RunStatus.COMPLETED, source="projector", evidence={})
    assert CONTRADICTED_KEY not in run.summary


def test_the_log_is_bounded_and_says_how_much_it_dropped() -> None:
    """出处日志是取证线索不是账本 —— 但截断这件事本身也要说出口，
    否则读起来就是"一共只发生过这些"。"""
    run = _run()
    for index in range(MAX_STATUS_LOG + 5):
        project_run_status(
            run,
            RunStatus.RUNNING if index % 2 else RunStatus.RETRYING,
            source="projector", evidence={"i": index},
        )
    assert len(run.summary[STATUS_LOG_KEY]) == MAX_STATUS_LOG
    assert run.summary["statusLogDropped"] == 5
