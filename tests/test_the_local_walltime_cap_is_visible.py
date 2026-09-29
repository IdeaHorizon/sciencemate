"""平台会杀掉作业的那条线，模型得先看得见。

## 病例（2026-09-07 真机第五轮）

experiment 规划了一次 100 个 (L,T) 点的蒙特卡洛扫描，其中 L=64 是 500 万测量步
—— 按实测速率总时长 2.5–3 小时。它调 `submit_job(scheduler="local", ...)`，
**没有传 walltime_minutes**，因为工具描述当时逐字写着：

    "Explicit SLURM/PBS hard walltime. **Omit for local jobs**; ..."

平台随后按 `_LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES = 60` 施加了一个 3600 秒的
硬上限（记录里如实写着 `walltime_source: platform_safety_default`），到点杀进程。

也就是说：**文案叫它别管，然后机制在一小时后把它的三小时作业杀掉。** 这不是模型
判断失误 —— 它照着契约做了，代价由它承担。同一形状见
`feedback_prompt_promise_needs_an_api` / issue #832。

## 判据

1. `walltime_minutes` 的描述里必须出现那个分钟数，并且说明**本地作业也适用**；
2. 参数是真管用的：本地作业传了就按传的算，不传才落到安全上限。第 2 条不是
   多余的 —— 只改文案而参数对本地无效，等于换一种方式撒谎。
"""
from __future__ import annotations

import pytest

from core.bootstrap import bootstrap
from core.tool_registry import get_tool
from nodes.experiment.tools.resource_manager import (
    _LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES,
)


@pytest.fixture(scope="module", autouse=True)
def _registry():
    bootstrap()


def _the_two_texts() -> dict[str, str]:
    """模型读 submit_job 的**两个**地方，分开取。

    `description` 是它挑工具时读的；`parameters_schema` 是它填参数时读的。
    合成一段来查，会让"只有一处说了真话"也算通过 —— 变异实测：只把能力清单那
    行改回旧文案，合并检查照样全绿。
    """
    tool = get_tool("submit_job")
    assert tool is not None, "submit_job 没注册 —— 这条判据无从谈起"
    import json

    return {
        "description": tool.description or "",
        "parameters_schema": json.dumps(tool.parameters_schema or {}, ensure_ascii=False),
    }


def _submit_job_text() -> str:
    return "".join(_the_two_texts().values())


@pytest.mark.parametrize("where", ["description", "parameters_schema"])
def test_the_cap_is_written_where_the_model_reads_it(where: str) -> None:
    """那个分钟数必须出现在模型读得到的**每一处**文字里。"""
    text = _the_two_texts()[where]
    assert str(_LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES) in text, (
        f"submit_job 的 {where} 里没有 {_LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES} 这个数 —— "
        "模型在这一处看不见平台会在哪一刻杀掉它"
    )


def test_the_contract_does_not_tell_the_model_to_ignore_it() -> None:
    """不许再写「本地作业省略它」。

    那正是这次事故的原句：模型照做，然后三小时的作业在一小时被杀。
    """
    text = _submit_job_text().lower()
    assert "omit for local jobs" not in text, (
        "契约还在叫模型对本地作业省略 walltime —— 而省略的后果是被平台杀掉"
    )
    assert "本地" in _submit_job_text() or "local" in text


def test_an_explicit_walltime_actually_applies_to_a_local_job() -> None:
    """参数得真管用：本地作业传了就按传的算，不传才落到安全上限。

    只改文案而参数对本地无效，等于换一种方式撒谎 —— 所以这条落在**行为**上。
    """
    from nodes.experiment.tools.resource_manager import the_local_walltime_seconds as wall

    assert wall(None, None) == _LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES * 60
    assert wall(180, None) == 180 * 60, "给了 180 分钟却不按它算"
    assert wall(1, None) == 60
    # 更硬的期限（节点自己算出来的 deadline）优先，单位是秒不是分。
    assert wall(180, 90) == 90
    assert wall(None, 90) == 90


def test_both_places_ask_the_same_function() -> None:
    """记录上写的期限和真正生效的期限，必须来自同一次计算。

    这条规则此前在两处各写了一遍（`sandbox_contract` 里一次、`SandboxLimits`
    里一次）。两份抄件只要有一份被改，作业就会在与记录不同的时刻被杀，而记录
    看上去完全正常。
    """
    import ast
    import pathlib

    import nodes.experiment.tools.resource_manager as rm

    tree = ast.parse(pathlib.Path(rm.__file__).read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "the_local_walltime_seconds"
    ]
    assert len(calls) >= 2, (
        f"只有 {len(calls)} 处在问这个函数 —— 另一处大概又自己算了一遍"
    )
    # 第一版这里数的是"这个常量被引用了几次，不许超过 4" —— 那是个魔数判据：
    # PR#840 往批准卡上加了一行**只是把它印出来**的文案，引用变成 5 次，判据当场
    # 转红，而什么都没坏。数出现次数从来不是我要问的事。
    #
    # 我要问的是：**除了那个函数，还有谁在拿它做算术**。把上限乘成秒、或按条件
    # 挑一个值再乘，都是"又算了一遍"；把它印进一句话里不是。
    owner = "the_local_walltime_seconds"
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == owner:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.BinOp):
                continue
            if any(
                isinstance(leaf, ast.Name)
                and leaf.id == "_LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES"
                for leaf in ast.walk(inner)
            ):
                offenders.append(f"{node.name}:{inner.lineno}")
    assert not offenders, (
        f"这些地方拿安全上限自己做了算术：{offenders} —— "
        f"这条规则只能由 `{owner}()` 回答，否则记录上写的期限和真正生效的期限会分叉"
    )
