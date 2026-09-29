"""harness.yaml 白名单里声明的工具，该节点必须真的拿得到。

## 为什么（2026-08-22 扫盘）

白名单是 owner 的声明；`allowed_node_types` 是框架的安全边界。两者冲突时
`list_tools_for_node` **静默取交集** —— owner 的声明被无声吞掉，而 harness.yaml
和 system_prompt 照旧点着那个工具的名。

扫盘当天有两处这样活着：

  · `experiment` 拿不到 `safe_execute_python` —— 那是**为它而生的**私有包装，
    因为 `dataclasses.replace(_orig_py_def, name="safe_execute_python", ...)`
    把 `allowed_node_types=["postprocess"]` 一起继承了过来。2026-08-08 引入，
    活了 14 天。能力上有 `safe_run_bash` 兜底，所以现场看不出异常 ——
    代价是 `stage` / `execution_params` 那层对账契约从未生效过。
  · `observation` 拿不到 `execute_python`，而它的 system_prompt 有一整节
    「你不做实验，但你会算」。

## 与 test_prompt_named_tools_exist 的分工

那条问的是「这个名字在不在 registry」，这条问的是「这个节点够不够得着」。
两个问题，两道闸 —— 第一道全绿的时候第二道正红着，这就是当时的现场。

## 判据

扫全部节点，`allowed_node_types` 冲突的差集必须为空。**不写豁免名单** ——
新节点、新工具一声明就自动进扫描面。

`required_runtime_capability` 不在判据里：那是运行时状态（writing 的 fixture
交付工具就该有时有、有时没有），按情况不授予是设计本身，不是配置错误。
"""
from __future__ import annotations

from core.bootstrap import bootstrap
from core.loader import NODES_DIR, list_harnesses, load_harness
from core.tool_registry import get_tool


def _blocked_tools(node_type: str, declared: list[str]) -> list[str]:
    """该节点声明了、但被 allowed_node_types 静态挡死的工具。"""
    out = []
    for name in declared or []:
        tool = get_tool(name)
        if tool is None or tool.internal_only:
            continue
        allowed = tool.allowed_node_types
        if allowed is not None and node_type not in allowed:
            out.append(f"{node_type}.{name} (allowed={sorted(allowed)})")
    return out


def test_every_declared_tool_is_reachable_by_its_node():
    bootstrap()
    offenders: list[str] = []
    for name in list_harnesses(NODES_DIR):
        harness = load_harness(name)
        offenders.extend(_blocked_tools(harness.node_type, harness.tools))

    assert not offenders, (
        "harness.yaml 白名单声明了、但该节点永远拿不到的工具：\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n\nlist_tools_for_node 会把它们静默滤掉，而 prompt 里照旧点着名。\n"
        "二选一：把该节点加进那个工具的 allowed_node_types，"
        "或者从白名单和 system_prompt 里一并删掉。"
    )


def test_the_gate_actually_fires(monkeypatch):
    """变异测试：把判据改坏一次，看这道闸红不红。

    「验证靠变异，不靠测试全绿」—— 没有这一条，上面那个 assert 有可能因为
    扫描面为空（比如 list_harnesses 返回 []）而永远绿。
    """
    from core import loader

    bootstrap()
    harness = load_harness("postprocess")
    # postprocess 独占科学制图落账入口，拿它当阳性样本。
    probe_tool = "render_figure"
    assert probe_tool in harness.tools

    blocked = _blocked_tools("literature", [probe_tool])
    assert blocked, f"literature 不在 {probe_tool} 的 allowed 里，判据必须报出来"

    # 且 load_harness 这道运行时闸也要真的拦
    import pytest

    fake = type(harness)(**{**harness.__dict__, "node_type": "literature"})
    with pytest.raises(RuntimeError, match="拿不到"):
        loader._refuse_if_declared_tools_are_not_granted(fake)
