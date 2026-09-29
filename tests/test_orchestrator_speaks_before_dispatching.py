"""派发节点之前必须先跟用户说一句 —— 而且是 schema 强制，不是 prompt 请求。

## 现场（wangd 2026-08-17）

一个真会话跑完 hypothesis → reviewer → curator → dreaming → writing，事件统计：

    assistant 消息      0 条
    调度器叙述          8 条
    子节点内部叙述     51 条
    工具卡           ~190 张

用户原话：「就没有看到调度器的任何的输出…连他妈一开始都不说话了？起码得让
用户了解现在在干啥吧」。

## 为什么不是"回复契约"能覆盖的

回复契约（chat.py）管的是**一轮终态**必须有干净文本。而这一轮跨了五个节点、
一个多小时还没结束 —— 契约一次都没触发。粒度错了：用户需要的是**每个决策点**
说一句，不是等一轮跑完。

## 为什么钉在 schema 上

prompt 里的「必须」不是机制。同一个仓库里的前车之鉴：hypothesis 的 prompt 把
"结束前必须 validate" 写了三遍，agent 全程 56 次工具调用一次没调。所以这里
把它放进 `required`，缺了就调不动，且报错直接说清要写什么、给例句。
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from core.state import State
from shared.tools.run_node import _run_node_tool


def _state() -> State:
    return State.new(node_type="_orchestrator", base_dir=Path(tempfile.mkdtemp()))


def test_dispatch_without_a_user_note_is_refused():
    """契约归 schema（判决拆除刀 1）：required 由派发口核，工具体内不再手写。"""
    from core.bootstrap import bootstrap
    from core.tool_registry import execute

    bootstrap()
    state = _state()
    result = asyncio.run(execute("run_node", state, node_type="literature"))
    assert result["status"] == "error"
    assert "user_note" in result["error"]
    # 契约要送到调用方：说清要写什么、并给范例 —— 现在住在 schema description 里，
    # 派发口报错时整份 schema 一并交回。
    desc = result["parameters_schema"]["properties"]["user_note"]["description"]
    assert "必填" in desc and "✅" in desc


def test_a_blank_user_note_does_not_satisfy_the_contract():
    """空白串不算说过话 —— 否则模型学会传 ' ' 绕过。minLength:1 按去空白核。"""
    from core.bootstrap import bootstrap
    from core.tool_registry import execute

    bootstrap()
    state = _state()
    result = asyncio.run(execute("run_node", state, node_type="literature", user_note="   \n "))
    assert result["status"] == "error"
    assert "user_note" in result["error"] and result.get("parameter_violations")


def _announcements(state) -> list[dict]:
    import json

    # 被闸门拒的派发**一个字都不写** —— transcript 文件可能压根不存在。
    if not state.transcript_path.exists():
        return []
    return [
        json.loads(line) for line
        in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line and json.loads(line).get("event") == "node_dispatch_announced"
    ]


def test_a_rejected_dispatch_never_announces_itself():
    """闸门挡下来的派发**不许**留下宣告 —— 那是没兑现的承诺。

    2026-08-18 实测（会话 c9deb4f2）：宣告原来发在函数开头，与真正的派发之间
    隔着 18 个 error 出口。writing 被宣告 17 次，磁盘上只有 1 条 writing run
    —— 对话里 16 句"我要去写论文了"，而它一次都没去。用户看到的是"怎么一直
    在说同一件事"。

    这里用 callable_nodes 白名单当那 18 个闸门的代表：它拒了，就不该有宣告。
    """
    state = _state()   # 裸 state：callable_nodes 为空 → 必被拒
    result = asyncio.run(_run_node_tool(
        state, node_type="literature",
        user_note="先做文献调研：项目里还没有证据基础，我需要先摸清既有研究。",
    ))
    assert result["status"] == "error"
    assert not _announcements(state), (
        "派发被闸门拒了却已经对用户宣告过 —— 承诺兑现不了"
    )


def test_a_dispatch_that_gets_through_does_announce():
    """过了闸门就必须留痕 —— 平台靠它投影成对话里那句话。"""
    from unittest.mock import patch

    state = _state()
    state.hook_state["_callable_nodes"] = ["*"]

    async def _blow_up_inside_the_dispatch(**kwargs):
        # 过了闸门、真的开始派了 —— 之后炸不炸不影响"承诺已经作数"。
        # 用异常而不是伪造 summary：summary 的契约很宽，伪造它等于在测试里
        # 重写一遍 executor。
        raise RuntimeError("dispatch reached the executor")

    with patch("core.executor.execute_node", _blow_up_inside_the_dispatch):
        try:
            asyncio.run(_run_node_tool(
                state, node_type="literature",
                user_note="先做文献调研：项目里还没有证据基础，我需要先摸清既有研究。",
            ))
        except RuntimeError:
            pass
    announced = _announcements(state)
    assert announced, "过了闸门的派发没留下宣告，平台就投影不出对话"
    assert announced[0]["node_type"] == "literature"
    assert "文献调研" in announced[0]["user_note"]


def test_the_parallel_path_carries_the_note_too():
    """并行派发是**另一条**调用路径 —— 护栏必须覆盖它。

    差点漏掉：`run_nodes_parallel` 把 job dict 拆成具名参数转给同一个 handler，
    但没传 user_note。后果不是"并行时可以不说话"（那还算轻），而是整条并行
    路径被契约全数拒掉，且症状是「并行怎么都起不来」，指不回这里。

    新增护栏时，第一件事是数清楚有几条路径能到达被守的那个点。
    """
    import inspect

    from shared.tools import run_node as module

    src = inspect.getsource(module._run_nodes_parallel)
    assert "user_note=job.get(\"user_note\")" in src, \
        "并行路径没把 user_note 传下去 —— 契约会把每个 job 都拒掉"

    # schema 侧也要必填：一个口子松了，护栏等于没有。
    from core.tool_registry import get_tool

    tool = get_tool("run_nodes_parallel")
    assert tool is not None
    job_schema = tool.parameters_schema["properties"]["jobs"]["items"]
    assert "user_note" in job_schema["required"]

    single = get_tool("run_node")
    assert single is not None
    assert "user_note" in single.parameters_schema["required"]
