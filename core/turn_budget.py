"""还剩几轮 —— 让节点自己看得见预算。

## 为什么加这个（E2E v23，2026-08-11 实测）

experiment 在第 40 轮撞上 `max_turns` 被切断：14 个 LAMMPS 模拟全跑完了，
**MSD 分析一行没做**。它在产出说明里写着「达到 max_turns=40 截断，未做 MSD
分析」—— 也就是说它是**事后**才知道有这个上限的。

`#398` 让这次截断对下游可见（决策层因此推荐 REVISE 接着做），把代价从
「空转三小时」降到「多绕一轮」。这里补更前面那一步：**让它一开始就知道有预算**。

知道「还剩 8 轮」的节点可以先把已算出的结果落成产物、把长任务拆两轮、或者
声明这一轮只做哪一段。不知道的节点只能一路往前，然后在任意位置被剪断 ——
剪在哪儿全看运气。

## 为什么不是「把 40 调大就完了」

调大只是把悬崖往后挪（默认值确实同时从 40 提到了 80，那是判断不是推导）。
真正的差别是**模型能不能规划**：预算可见时，撞上限从"意外"变成"可以提前
避开的事"。

## 只在快没了的时候说

每轮都播报 = 噪音，模型会开始无视它 —— 今晚 `dreaming_due` 就是这么被无视的
（连播 19 次，一次都没被响应）。按剩余比例分档，越紧越具体。
"""

from __future__ import annotations

#: 分档阈值：剩余比例低于它就开始提醒。
_HALF = 0.5
_QUARTER = 0.25
#: 剩这么几轮时给最具体的指令（比例档在小预算下会失真：80×0.1=8，但 20×0.1=2）。
_FINAL_STRETCH = 5


def budget_notice(*, turn, max_turns) -> str | None:
    """这一轮要不要提醒预算；要就返回提示文本。

    算不出来一律返回 None —— 观察不能打断主流程，也不该在无上限的长 dogfood
    里凭空冒出一条"你快没轮次了"。
    """
    try:
        used = int(turn)
        cap = int(max_turns)
    except (TypeError, ValueError):
        return None
    if cap <= 0 or used <= 0 or used > cap:
        return None

    left = cap - used
    if left <= _FINAL_STRETCH:
        return (
            f"⏳ **只剩 {left} 轮**（第 {used}/{cap} 轮）。撞到上限会被**当场切断**，"
            "半途的工作不会有收尾轮次。\n"
            "现在就把**已经做完的部分落成产物**（哪怕是阶段性的），并在产出里"
            "写清哪些做完了、哪些没做 —— 下一轮由谁接着做，框架看得见才接得上。"
        )
    ratio = left / cap
    if ratio <= _QUARTER:
        return (
            f"⏳ 还剩 {left} 轮（第 {used}/{cap}）。别再起新的长任务；"
            "把手上的收尾、落产物。"
        )
    if ratio <= _HALF:
        return (
            f"⏳ 轮次过半（第 {used}/{cap}，还剩 {left}）。"
            "估一下剩下的活够不够 —— 不够就现在拆，别拖到被切断。"
        )
    return None


#: 无上限。`HARNESS_DEFAULT_MAX_TURNS=0` 与"节点没声明"都落到这里。
UNLIMITED = 0

#: 循环里用的哨兵（Python 没有真无限 range）。
_PRACTICALLY_UNLIMITED = 10 ** 9


def resolve_cap(*, mode_turns: int, env_default: str | None) -> int:
    """本 run 实际生效的轮次上限。0 = 无上限。

    ## 默认无上限（2026-08-11 决定）

    这个数字原本是 40，来自审计高危 #6：当时 `HARNESS_DEFAULT_MAX_TURNS=0`
    下空转 ~40 分钟 / ~5300 次失败调用，把 conversation 灌到 138K token 无法
    resume。但**那次事故的真凶是 tool-call 协议故障循环**，而它后来有了自己的
    专用熔断（`ProtocolFailureBreaker`，代码里明确写着"不看 max_turns"）。

    于是 40 就只剩下惩罚"活儿本来就多"的节点。实测代价（E2E v23）：

        9 次 run 里 **6 次**正好停在 40 —— experiment ×2、_curator ×3、
        _reviewer ×1，全部记成 completed；
        hypothesis / literature / data / postprocess / writing **一次没跑过**；
        没有图、没有论文、research_state 停在 v1。

    **轮数根本区分不了"跑得久"和"原地转"**，而进展类护栏能：协议熔断、空轮
    上限（`_VOID_COMPLETION_MAX`）、乒乓熔断、停滞指纹、重复失败熔断。
    所以让轮数彻底退出"防空转"这件事。

    两个逃生口保留：`HARNESS_DEFAULT_MAX_TURNS=<N>` 想设仍能设；
    节点 yaml 显式 `max_turns > 0` 永远优先。
    """
    if mode_turns and mode_turns > 0:
        return int(mode_turns)
    try:
        value = int(env_default) if env_default not in (None, "") else UNLIMITED
    except (TypeError, ValueError):
        value = UNLIMITED
    return value if value > 0 else UNLIMITED


def loop_bound(cap: int) -> int:
    """把上限翻译成 `range()` 能用的数 —— 无上限时给一个够大的哨兵。"""
    return cap if cap and cap > 0 else _PRACTICALLY_UNLIMITED
