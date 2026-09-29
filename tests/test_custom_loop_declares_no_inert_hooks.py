"""换掉框架 loop 的节点，不许再声明它永远不会跑的 loop_hooks。

## 为什么需要这条（2026-08-31）

`loop_hooks` 只由 `core.agent_loop.run_loop` 触发（`run_on_turn_start` /
`run_on_end` 全仓只有那一个调用方）。而 `core/executor.py` 对声明了
`custom_loop:` 的节点是**直接 dispatch**：

    if custom is not None:
        loop_result = await custom(harness, state, messages, llm)
    else:
        loop_result = await run_loop(harness, state, messages, llm)

也就是说，custom loop 节点的 `loop_hooks:` 一行都不会执行。

这本来没人踩，因为 data 的 custom loop 内部还会调 `run_default`——hook 借那条
路照跑。`codex/data-sync` 把 `run_default` 整个删掉之后，那条路没了：同一份
`loop_hooks:` 从「生效」变成「装饰」，**而 yaml 一个字都没改，也不会有任何
报错**。三道"每个节点都该接 X"的扫盘闸（定向层 / scratchpad / 第二意见）因此
拿到了机械豁免（见各自 docstring）。

豁免必须配一道对称的闸，否则它就是一个可以躺假配置的洞：以后谁把自己的节点
换成 custom loop，既拿到了豁免、又能继续在 yaml 里写着 hook —— 读 yaml 的人
会以为那道防线在场。**声明只许写会跑的东西。**

## 判据

扫全部 harness：`not runs_the_framework_loop(node) and harness.loop_hooks` → 红。
判据问的是「这趟真正跑哪个 loop」—— agent_loop.py 在、但没在
`framework_exemptions.yaml` 登记（或 import 失败）的节点，框架照样走默认 loop、
hook 会跑，不该被豁免。
不写节点名单；今天只有 data 命中这个形状，明天谁换 loop 谁自动进扫描面。

真要在 custom loop 里做 hook 那件事，就在自己的 loop 里显式调它 —— data 的
`run_loop` 对结构文件校验就是这么做的（`validate_structure_outputs` 在 finally
里兜底，由 `test_redirect_pingpong.py::test_data_custom_loop_wraps_for_guaranteed_validation`
把关）。显式调用看得见、测得到；yaml 里那一行看不出来跑没跑。
"""
from __future__ import annotations

from core.custom_loop import runs_the_framework_loop
from core.loader import list_harnesses, load_harness


def test_a_custom_loop_node_declares_no_hooks_the_framework_will_never_fire() -> None:
    inert: dict[str, list[str]] = {}
    for node in list_harnesses():
        if runs_the_framework_loop(node):
            continue
        declared = list(load_harness(node).loop_hooks or [])
        if declared:
            inert[node] = declared
    assert not inert, (
        "这些节点换掉了框架 loop，声明的 loop_hooks 一次都不会被触发 —— "
        "要么把它们从 harness.yaml 删掉，要么在自己的 loop 里显式调用：\n"
        + "\n".join(f"  {node}: {hooks}" for node, hooks in sorted(inert.items()))
    )


def test_the_framework_still_fires_hooks_for_everyone_else() -> None:
    """反向锚：这条豁免只对 custom loop 成立，别的节点该有的照样得有。

    没有这一条，上面那条测试单独看像是在说「loop_hooks 可有可无」。
    """
    hooked = [n for n in list_harnesses()
              if runs_the_framework_loop(n) and (load_harness(n).loop_hooks or [])]
    assert hooked, "跑框架 loop 的节点里一个声明 loop_hooks 的都没有 —— 判据多半选错了"
