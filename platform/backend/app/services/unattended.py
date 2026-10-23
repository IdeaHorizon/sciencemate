"""无人值守之前，先问这台机器守不守得住。

## 现场（issue #798）

`enforcement_record` 一直在算 `missing_for_unattended` —— 这台机器离"能放着不管
地跑"还差哪几条不变量。它被记进 `app.state.execution_boundary`、被
`/health/ready` 交出去、被写进日志……**然后没有任何人读它做决定**。

一个算出来没人消费的判据，等于那道防线不在场。自主档照样能开，作业照样在一台
守不住写边界的机器上跑起来，而"我们知道它守不住"这件事只存在于一行日志里。

## 判据分两档，因为缺的东西性质不同

- **不可恢复**（写边界 / .git 不可写 / 断网）：缺了就不是"弱一点"，是这台机器
  上根本没有边界。放着不管地跑 = 把整台机器交给模型。拒绝，并说清楚缺什么。
- **可恢复**（内存 / 进程数）：缺了是"跑飞的时候没人踩刹车"。在
  **自己的电脑**上这是用户自己的机器、自己看着，标注一下放行；在组织的共享
  执行器上不行 —— 那里跑飞会踩到别人。

这两档的区别不是我们发明的：`core.isolation.UNRECOVERABLE` 早就这么分好了。
这里只是终于有人按它做决定。

**一条例外，按发行档位分**（#931，wangd 2026-10-21）：断网在**用户自己的机器**上降为
可恢复 —— 外泄的对象与承担者是同一个人；组织的共享执行器上仍不可恢复。写边界与 .git
两边都不让步。见 `RECOVERABLE_ON_YOUR_OWN_MACHINE`。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: 缺了就没有边界可言的那几条。与 `core.isolation.UNRECOVERABLE` 同一份名单 ——
#: 这里按值（字符串）写，因为记录跨进程传过来的就是字符串。
UNRECOVERABLE = frozenset({"write_boundary", "git_unwritable", "net_deny"})

#: 在**用户自己的机器**上降为可恢复的那几条（#931，RFC_CAPABILITY_GRANTS_LAYERING D9）。
#:
#: 断网守的是「数据不外泄」。个人档那台机器是用户自己的，跑的是他自己点开的 agent，
#: 外泄的对象与承担者是同一个人 —— 这和「写边界 / .git 不可写」不同：那两条缺了，
#: 他的文件和历史会**在他看不见的时候**被改掉或分叉，所以照旧不可恢复。
#: 组织的共享执行器上断不掉网仍是不可恢复：那里的数据不只是他一个人的。
#: 此前 Windows 上 `net_deny` 永远缺（MSYS bash 进不了 AppContainer），于是每一台 Windows
#: 都被判「不能无人值守」，长课题跑到每个 turn 边界就停下等人 —— 把一条本该由用户承担的
#: 风险，做成了所有个人用户的硬停机。
RECOVERABLE_ON_YOUR_OWN_MACHINE = frozenset({"net_deny"})

_HOW_TO_FIX = {
    "write_boundary": "这台机器没有可用的沙箱后端（macOS 需要 sandbox-exec，"
                      "Linux 需要内核 ≥ 5.13 的 Landlock 或 bubblewrap）",
    "git_unwritable": "沙箱挡不住对 .git 的写入",
    "net_deny": "沙箱断不掉网络",
    "mem_cap": "内存上限",
    "pids_cap": "进程数上限",
    "cpu_cap": "CPU 配额",
    "walltime": "墙钟上限",
    "group_kill": "进程组回收",
}


@dataclass(frozen=True)
class UnattendedVerdict:
    """能不能放着不管地跑，以及为什么。"""

    allowed: bool
    #: 拒绝时给人看的一句话：缺什么、为什么这条不能让步。
    reason: str = ""
    #: 放行但要标注：跑飞的时候没人踩刹车，用户自己看着。
    note: str = ""


def judge_unattended(
    missing_for_unattended: Iterable[str],
    *,
    weak_resource_walls_are_acceptable: bool,
) -> UnattendedVerdict:
    """`missing_for_unattended` → 一个判决。

    `weak_resource_walls_are_acceptable` 是"这台机器是不是用户自己的"：个人档
    为真（自己的电脑、自己看着），组织的共享执行器为假（跑飞会踩到别人）。
    在自己的机器上，`RECOVERABLE_ON_YOUR_OWN_MACHINE` 里的那几条（断网）从不可恢复
    降为"标注后放行"；写边界与 .git 两边都不让步。
    """
    missing = sorted({str(item) for item in missing_for_unattended if str(item).strip()})
    if not missing:
        return UnattendedVerdict(allowed=True)

    blocking = [
        item for item in missing
        if item in UNRECOVERABLE
        and not (weak_resource_walls_are_acceptable and item in RECOVERABLE_ON_YOUR_OWN_MACHINE)
    ]
    if blocking:
        what = "、".join(_HOW_TO_FIX.get(item, item) for item in blocking)
        return UnattendedVerdict(
            allowed=False,
            reason=(
                f"这台机器守不住无人值守所需的边界：{what}。"
                "放着不管地跑等于把整台机器交给模型，所以无人值守续轮不开。"
                "档位照旧：自主 / 连续下的决策点仍按档位自动放行，只是这一轮结束后不会自己开下一轮。"
            ),
        )

    weak = "、".join(_HOW_TO_FIX.get(item, item) for item in missing)
    if weak_resource_walls_are_acceptable:
        note = f"这台机器没有{weak}：跑飞的时候没有自动刹车，留意一下资源占用。"
        if "net_deny" in missing:
            # 说实话、说后果：模型跑的命令在这台机器上能上网。不是"弱一点"，是这一条
            # 由用户自己承担（机器是他的）；写边界与 .git 照旧守着。
            note = (
                "这台机器的沙箱断不掉网络：模型跑的命令能上网（机器是你自己的，由你承担；"
                "写边界与 .git 不可写照旧守着）。"
                + (f"另外没有{'、'.join(_HOW_TO_FIX.get(i, i) for i in missing if i != 'net_deny')}："
                   "跑飞的时候没有自动刹车，留意一下资源占用。"
                   if len(missing) > 1 else "")
            )
        return UnattendedVerdict(allowed=True, note=note)
    return UnattendedVerdict(
        allowed=False,
        reason=(
            f"这台共享执行器没有{weak}，无人值守跑飞会影响别人，所以这一轮结束后不会自己开下一轮。"
            "决策点仍按档位自动放行。让管理员补上资源限制就能续轮。"
        ),
    )
