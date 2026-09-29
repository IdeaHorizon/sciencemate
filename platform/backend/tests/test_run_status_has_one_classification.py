"""RunStatus 的语义分类只能有一份。

## 为什么需要这组测试

2026-08-10 普查：全仓有**五份**各自手写的 RunStatus 分区，散在五个文件里，
互相不一致。今晚三个缺陷全出自这里：

  · 回收器只扫 waiting_human → `running` 的尸体漏掉（PR #359）
  · 恢复只认 stale_unknown  → 平台越及时发现故障，用户越恢复不了
  · 活性探针只认两个状态     → 上层修了，下层名单没跟着改

共同形状：**某一份名单写漏或写反，而分叉时两边都不报错。** 一边照发，一边
按旧名单解析，解析不到就当"没有"。

根因不是某份名单错了，是 13 个状态值没有权威分类，于是每个消费者自己写一遍。
这组测试的作用不是检查现在对不对，而是**让"新增一个状态却不分类"变成红灯**。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.models.execution import (
    CLEANLY_FINISHED_RUN_STATUSES,
    REQUIRES_LIVE_RUNTIME_STATUSES,
    TERMINAL_RUN_STATUSES,
    UNFINISHED_RUN_STATUSES,
    RunStatus,
)

#: 刻意既不是终态、也不要求活进程的两个。加进来必须写清理由。
DELIBERATE_EXCEPTIONS = {
    # 外部作业在集群排队时 harness 进程本来就可以不在（作业由调度器持有），
    # 把它算成"需要活进程"会把正常等待判成故障。
    RunStatus.WAITING_COMPUTE,
    # 运行时已经丢了（所以不要求活进程），但活没干完（所以不是终态）。
    RunStatus.STALE_UNKNOWN,
}


def test_every_status_is_classified_or_explicitly_excepted() -> None:
    """新增一个状态值而不分类 → 这条立刻红。

    这就是这组测试存在的全部理由：把"漏了一个"从**静默错误**变成**编译期
    级别的提醒**。
    """
    classified = TERMINAL_RUN_STATUSES | REQUIRES_LIVE_RUNTIME_STATUSES
    unclassified = set(RunStatus) - classified - DELIBERATE_EXCEPTIONS
    assert not unclassified, (
        f"这些状态既不是终态也没说要不要活进程：{sorted(s.value for s in unclassified)}。"
        "在 app/models/execution.py 里给它分类，或写进本文件的 DELIBERATE_EXCEPTIONS "
        "并说明理由。"
    )


def test_terminal_and_live_runtime_do_not_overlap() -> None:
    """终态不可能同时要求活进程 —— 重叠说明某一边理解错了。"""
    overlap = TERMINAL_RUN_STATUSES & REQUIRES_LIVE_RUNTIME_STATUSES
    assert not overlap, sorted(s.value for s in overlap)


def test_unfinished_is_exactly_terminal_minus_clean_plus_stale() -> None:
    """"没干完"必须由另外两类推出来，不能是第三份独立名单。

    否则又是三份可以各自演化的集合。
    """
    derived = (TERMINAL_RUN_STATUSES - CLEANLY_FINISHED_RUN_STATUSES) | {
        RunStatus.STALE_UNKNOWN
    }
    assert UNFINISHED_RUN_STATUSES == derived, (
        f"UNFINISHED 应当等于 终态-干净收尾+stale_unknown；"
        f"实际 {sorted(s.value for s in UNFINISHED_RUN_STATUSES)} "
        f"vs 推导 {sorted(s.value for s in derived)}"
    )


def test_clean_finish_is_a_subset_of_terminal() -> None:
    assert CLEANLY_FINISHED_RUN_STATUSES <= TERMINAL_RUN_STATUSES


def test_no_module_rebuilds_its_own_run_status_partition() -> None:
    """全仓不许再出现第二份手写分区。

    扫的是**源码**而不是行为：一份写错的名单在测试里往往仍然"能跑"，
    只有在特定状态出现时才暴露。所以直接禁止这种写法。
    """
    root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "execution.py" and path.parent.name == "models":
            continue  # 权威定义所在
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Set | ast.List | ast.Tuple):
                continue
            members = [
                ast.unparse(item)
                for item in node.elts
                if "RunStatus." in ast.unparse(item)
            ]
            # 两个以上 RunStatus 成员凑成的字面量集合 = 又一份分区
            if len(members) >= 3:
                offenders.append(
                    f"{path.relative_to(root)}: {', '.join(members[:6])}"
                )
    assert not offenders, (
        "又出现了手写的 RunStatus 分区。用 app/models/execution.py 里的"
        "TERMINAL / REQUIRES_LIVE_RUNTIME / UNFINISHED，别再抄一份：\n  "
        + "\n  ".join(offenders)
    )
