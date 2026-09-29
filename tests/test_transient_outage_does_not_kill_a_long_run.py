"""一次瞬时 5xx 不该杀掉一轮跑了几小时的实验。

## 现场（2026-08-10 E2E v25）

一轮无人值守研究跑了约 3 小时，hypothesis 冻了预注册，experiment 做完
**3 个真实 LAMMPS 生产模拟**（T=1.00 两个重复 + T=0.80 一个）。然后模型后端
返回一次 **HTTP 503**，整轮死掉。

事后我直接探那个端点：**HTTP 200，2.1 秒，回 pong** —— 它只是当时短暂不可用。

## 根因：预算的**单位**错了

`core/llm.py` 里治过一次同款问题，但**只治了 429**：

    这是并发位被占满…用 1s/2s/4s 去重试，三次加起来等 7 秒，必然三次全撞墙，
    然后整个 run 判死。退避得按"对方那次生成多久跑完"的量级来：5/10/20/40/80s

结论对，但只应用到了 429。5xx 留在 1s/2s/4s = **总共扛 7 秒**，理由写的是
"5xx 是服务端打了个嗝，秒级重试就够"。而 5xx 的真实成因是 GPUStack 节点重启 /
模型重载 / OOM 后拉起 —— 那是**几十秒到几分钟**。

更根本的是：`max_retries` 是按"**一次 API 调用**"设的预算。一次闲聊和一个
跑了 3 小时、做完 3 个真实模拟的实验，共用同一个 3 次 / 7 秒。真正该问的是
**"这次失败会损失多少，所以值得扛多久"**。

## 修法

1. 5xx 的阶梯与 429 同量级（起步 5s、封顶 60s、次数下限 5）→ 扛 ~140 秒
2. 加一层**时间预算**，与次数上限并行，谁先到都停
3. 平台起的 harness 子进程拿 600s（已投入几小时的 run），CLI 一次性调用 60s
"""
from __future__ import annotations

import inspect

from core import llm


def test_five_xx_rides_out_more_than_a_couple_of_seconds() -> None:
    """按新阶梯，前 5 次重试累计要能扛过一次后端重启（≥100 秒）。

    旧阶梯（1/2/4）总共 7 秒 —— 这个数字就是那轮实验的死因。
    """
    total = sum(
        llm._compute_backoff(i, 1.0, None, rate_limited=False) for i in range(5)
    )
    assert total >= 100, f"5xx 只能扛 {total:.0f}s，一次节点重启就死"


def test_the_default_carries_the_policy_not_a_hidden_floor() -> None:
    """"5xx 也要扛得久"由**默认值**表达，不由归一化函数偷偷抬高别人给的数。

    第一版我在 `_effective_max_retries` 里给 5xx 也加了下限，把 per-call
    override 打没了（传 1 却打了 6 次），被既有用例当场证伪。
    """
    # 显式意图必须赢。
    assert llm._effective_max_retries(1, rate_limited=False) == 1
    assert llm._effective_max_retries(3, rate_limited=False) == 3
    assert llm._effective_max_retries(0, rate_limited=False) == 0
    # 429 的既有放宽保持不变。
    assert llm._effective_max_retries(3, rate_limited=True) >= 5
    # 策略在默认值上：不传就给足够扛过一次重启的次数。
    client = llm.LLMClient(api_key="k", model="m", base_url="http://x")
    assert client.max_retries >= 5


def test_a_caller_supplied_base_cannot_shrink_the_ladder() -> None:
    """调用方传个小 base 不该把阶梯打回"打个嗝"的量级。

    原来 `base` 直接用调用方给的值，于是 harness 默认的 1.0 让 5xx 回到 1/2/4。
    """
    assert llm._compute_backoff(0, 0.1, None, rate_limited=False) >= 5


def test_time_budget_runs_in_parallel_with_the_attempt_cap() -> None:
    """两个闸并行，谁先到都停。

    次数防"对方永久 500 时无限打"；时间防"预算按一次 API 调用设、却要保护
    一个已经跑了几小时的 run"。
    """
    source = inspect.getsource(llm)
    assert source.count("attempt >= budget or time.monotonic() >= _deadline") == 2, (
        "两个重试循环（流式 / 非流式）都要接时间预算，漏一个就等于没接"
    )
    assert llm.transient_retry_budget_s(600) == 600.0
    assert llm.transient_retry_budget_s(None) > 0


def test_long_running_sessions_actually_get_the_bigger_budget() -> None:
    """接到路径上 —— 加了参数没人传，等于没加。"""
    from pathlib import Path

    source = Path(
        "platform/backend/app/services/harness_sessions.py"
    ).read_text(encoding="utf-8")
    assert '"LLM_RETRY_BUDGET_SECONDS"' in source
    assert '"600"' in source


def test_client_exposes_the_budget() -> None:
    client = llm.LLMClient(api_key="k", model="m", base_url="http://x", retry_budget_s=600)
    assert client.retry_budget_s == 600
    assert llm.LLMClient(api_key="k", model="m", base_url="http://x").retry_budget_s is None
