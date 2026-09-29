"""任务要有一个不可变的身份，和一条不可改写的合同（#1080）。

## 三件事此前都不成立

**一、`Txx` 当不了身份。** `_next_id()` 读全表取 max+1，`_persist` 裸 append ——
两个进程之间是个教科书式的 read-modify-write 竞态。issue 给的复现：4 个进程各
`create` 25 次，jsonl 每次都是 100 行，`list_all()` 只剩 53–57 条，**后写的把先写的
整条抹掉，而两条记的是两件不同的事**。本地部署下同一用户在同一项目开两个会话，
就是两个进程写同一个 tasks.jsonl。

**二、身份传不进子 run。** `run_node` 收一个可选的 `task_id`，拿它改 Task 状态、
记进父 run 的 flow，但**不放进 `execute_node` 的参数**。child State 和 run_start
事件里一个任务字段都没有。

**三、续跑不看任务。** 自动续跑挑「同 node_type、同 session、有 checkpoint」的最后
一个，续上之后这次的 node_inputs 覆盖进 hook_state，还提示模型「不一致以这条为准」
—— 于是**同一个 run_id 先后服务两个不同的任务**（#1052 的翼型会话）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from core.task_contract import (
    ASSIGNMENT_EXACT,
    ASSIGNMENT_NONE,
    ASSIGNMENT_PENDING,
    PreregAssignment,
    TaskContractError,
    TaskContractLog,
)
from core.tasks import TaskList


# ── 验收 1：并发创建 ────────────────────────────────────────────────────────

_REPRO = '''
import sys, tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core.tasks import TaskList

def worker(args):
    d, w = args
    tl = TaskList(Path(d))
    return [tl.create(f"w{w}-{i}", "", "experiment", f"r{w}").id for i in range(25)]

if __name__ == "__main__":
    d = sys.argv[2]
    with ProcessPoolExecutor(4) as ex:
        ids = [i for batch in ex.map(worker, [(d, w) for w in range(4)]) for i in batch]
    tl = TaskList(Path(d))
    rows = sum(1 for l in (Path(d)/"tasks.jsonl").read_text().splitlines() if l.strip())
    tasks = tl.list_all()
    print(rows, len(tasks), len(set(ids)), len({t.task_instance_uuid for t in tasks}))
'''


def test_concurrent_creates_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """issue 里那段复现必须转绿：100 次创建 = 100 条任务、100 个身份。"""
    script = tmp_path / "repro.py"
    script.write_text(_REPRO, encoding="utf-8")
    repo = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run(
        [sys.executable, str(script), repo, str(tmp_path / "tasks")],
        capture_output=True, text=True, timeout=180, check=True,
        env={"PYTHONPATH": repo, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
             "HOME": str(tmp_path)},
    )
    rows, restored, aliases, uuids = (int(x) for x in proc.stdout.split())
    assert rows == 100
    assert restored == 100, f"100 行只还原出 {restored} 条 —— 又被别名覆盖了"
    assert aliases == 100, f"只发出了 {aliases} 个不同别名"
    assert uuids == 100, f"只有 {uuids} 个不同身份"


def test_the_alias_is_not_the_identity(tmp_path: Path) -> None:
    tl = TaskList(tmp_path / "tasks")
    t = tl.create("做一件事", "", "experiment", "r1")
    assert t.task_instance_uuid and t.task_instance_uuid != t.id
    assert tl.get(t.task_instance_uuid) is not None, "按身份取不到，那身份就没用"
    assert tl.get(t.id) is not None, "别名也得还认得 —— 人读的是它"


# ── 验收 2：合同只追加，没有「取最新」 ──────────────────────────────────────


def _log(tmp_path: Path) -> TaskContractLog:
    return TaskContractLog(tmp_path / "tasks")


def test_a_revision_is_never_rewritten(tmp_path: Path) -> None:
    log = _log(tmp_path)
    first = log.append(task_instance_uuid="u1", objective="测 Tc", actor="a")
    second = log.append(task_instance_uuid="u1", objective="测 Tc 与误差",
                        actor="a", parent_revision_digest=first.digest)

    assert log.get("u1", first.digest).objective_digest == first.objective_digest, (
        "旧那版被新版改写了 —— 这本账的全部意义就是它不会")
    assert second.parent_revision_digest == first.digest
    assert len(log.revisions_for("u1")) == 2


def test_a_concurrent_fork_is_never_silently_resolved(tmp_path: Path) -> None:
    """同一个 parent 并发写两条：要么被明确拒绝，要么两条都在 —— 不许静默覆盖。"""
    log = _log(tmp_path)
    base = log.append(task_instance_uuid="u1", objective="base", actor="a")

    log.append(task_instance_uuid="u1", objective="甲的改法", actor="jia",
               parent_revision_digest=base.digest)
    with pytest.raises(TaskContractError) as e:
        log.append(task_instance_uuid="u1", objective="乙的改法", actor="yi",
                   parent_revision_digest=base.digest)
    assert "并发分叉" in str(e.value)

    forked = log.append(task_instance_uuid="u1", objective="乙的改法", actor="yi",
                        parent_revision_digest=base.digest, allow_fork=True)
    siblings = log.children_of("u1", base.digest)
    assert len(siblings) == 2, "显式分叉之后两条都得在"
    assert log.get("u1", forked.digest) is not None


def test_there_is_no_latest_revision_api() -> None:
    """没有「取最新」—— 分叉时"最新"不是一个有定义的东西。

    给一个 `latest()` 等于在分叉面前替调用方抽签，而那正是 LWW 的病换个地方再犯。
    """
    public = {name for name in dir(TaskContractLog) if not name.startswith("_")}
    assert not {n for n in public if "latest" in n or "current" in n or "head" in n}, public
    assert "get" in public and "children_of" in public


def test_an_absent_assignment_is_pending_not_none(tmp_path: Path) -> None:
    """缺席 ≠ 明确不绑。把它读成 explicit_none 就是替上游做了那个决定。"""
    log = _log(tmp_path)
    pending = log.append(task_instance_uuid="u1", objective="x", actor="a")
    assert pending.assignment is None
    assert pending.assignment_kind == ASSIGNMENT_PENDING

    bound = log.append(task_instance_uuid="u2", objective="x", actor="a",
                       prereg_assignment=PreregAssignment.exact(
                           "pre_registration__H1", version="2", content_hash="abc"))
    assert bound.assignment_kind == ASSIGNMENT_EXACT
    assert bound.assignment.artifact_id == "pre_registration__H1"

    none = log.append(task_instance_uuid="u3", objective="x", actor="a",
                      prereg_assignment=PreregAssignment.none("这趟只是装环境"))
    assert none.assignment_kind == ASSIGNMENT_NONE


def test_explicit_none_needs_a_reason() -> None:
    """「明确不绑」是一个决定 —— 没有理由的它和忘了填长得一模一样。"""
    with pytest.raises(TaskContractError):
        PreregAssignment.none("")
    with pytest.raises(TaskContractError):
        PreregAssignment.exact("")


# ── 验收 6：两本账互不改写 ──────────────────────────────────────────────────


def test_task_status_changes_write_no_contract_revision(tmp_path: Path) -> None:
    tl = TaskList(tmp_path / "tasks")
    log = _log(tmp_path)
    t = tl.create("做一件事", "", "experiment", "r1")
    log.append(task_instance_uuid=t.task_instance_uuid, objective="做一件事", actor="a")
    before = len(log.revisions_for(t.task_instance_uuid))

    tl.start(t.id, owner_node="experiment")
    tl.block(t.id, reason="缺输入") if hasattr(tl, "block") else None
    tl.complete(t.id)

    assert len(log.revisions_for(t.task_instance_uuid)) == before, (
        "改 status 写出了合同 revision —— 两本账开始互相改写了")


def test_appending_a_revision_does_not_touch_task_status(tmp_path: Path) -> None:
    tl = TaskList(tmp_path / "tasks")
    log = _log(tmp_path)
    t = tl.create("做一件事", "", "experiment", "r1")
    tl.start(t.id, owner_node="experiment")

    log.append(task_instance_uuid=t.task_instance_uuid, objective="改了主意", actor="a")
    assert tl.get(t.id).status == "in_progress"


def test_the_two_ledgers_are_two_files(tmp_path: Path) -> None:
    """合同不混进 tasks.jsonl —— 混进去就得共用那条 last-write-wins 的路。"""
    tl = TaskList(tmp_path / "tasks")
    log = _log(tmp_path)
    t = tl.create("x", "", "experiment", "r1")
    log.append(task_instance_uuid=t.task_instance_uuid, objective="x", actor="a")

    rows = (tmp_path / "tasks" / "tasks.jsonl").read_text(encoding="utf-8")
    assert "task_contract" not in rows and "objective_digest" not in rows
    assert (tmp_path / "tasks" / "task_contract_revisions.jsonl").exists()
