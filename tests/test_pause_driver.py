"""人选了 REVISE / REDIRECT 之后，运行时到底有没有把那个节点起起来。

`_execute_authorized_action` 是这条路上唯一执行人的决定的地方。它此前只接
**异常**：`_run_node_tool` 被拒绝时不抛异常，它 `return {"status": "error"}`
（输入契约不满足、callable 白名单、重复失败熔断、redirect 踢皮球……都走这条），
于是"没起来"一路落到 `return True` 被记成"起来了"——pause 照常 resume、
`authorized_action_dispatch_failed` 不写、`action_last_failure` 为空。

活体（#1082，09-15 node20 benchmark，4 次全同形）：授权到 resume 只隔 40 多
毫秒，而运行时派发是同步 await 的 —— 真起了子 run 不可能这么快。四个子 run
都是调度器在下一轮自己补起的，`human_directive` 那条通道结构上到不了节点。

两条判据，缺一条这个缺陷就能原样活着：
  1. 返回值带回来的失败和抛出来的失败是同一个答案的两种写法，两种都要认；
  2. 重跑要带着**原任务**走 —— 只有反馈、没有 experiment_spec 的重跑，正是
     契约检查拒绝它的原因。
"""
from __future__ import annotations

import pytest


class _State:
    def __init__(self):
        self.transcript: list[tuple[str, dict]] = []
        self.hook_state: dict = {}

    def append_transcript(self, kind, **kw):
        self.transcript.append((kind, kw))


class _Ctx:
    def __init__(self):
        self.state = _State()


def _authorized_entry(**extra) -> dict:
    entry = {
        "decision_state": "action_authorized",
        "authorized_target_node": "experiment",
        "authorized_action": "REVISE",
        "producing_run_id": "run_prod_1",
        "recommended_feedback": "reviewer: L=64 那一档缺收敛判据。",
    }
    entry.update(extra)
    return entry


@pytest.mark.asyncio
async def test_authorized_action_dispatch_error_result_is_not_reported_as_success(monkeypatch):
    """`_run_node_tool` 返回 status=error（不抛异常）时，这次派发算失败。"""
    from core import pause_driver
    import shared.tools.run_node as run_node_mod

    async def _rejecting_run_node(state_, target, **kw):
        return {
            "status": "error",
            "error": (f"node_inputs 的键与 {target!r} 声明的输入契约完全不匹配。"),
            "received_keys": ["human_directive", "mode", "reviewer_feedback"],
        }

    monkeypatch.setattr(run_node_mod, "_run_node_tool", _rejecting_run_node)

    ctx, entry = _Ctx(), _authorized_entry()
    ok = await pause_driver._execute_authorized_action(ctx, entry)

    assert ok is False, "目标节点没跑起来，却回报执行成功 —— pause 会照常 resume"
    assert entry.get("action_last_failure"), "失败没留痕：下一次呈递说不出为什么"
    assert "输入契约" in entry["action_last_failure"], entry["action_last_failure"]
    assert [k for k, _ in ctx.state.transcript if k == "authorized_action_dispatch_failed"], (
        f"事件里没有派发失败：{[k for k, _ in ctx.state.transcript]}"
    )


@pytest.mark.asyncio
async def test_a_raised_failure_is_still_a_failure(monkeypatch):
    """对照：抛异常那一侧原来就认，补返回值不能把它弄丢。"""
    from core import pause_driver
    import shared.tools.run_node as run_node_mod

    async def _raising_run_node(state_, target, **kw):
        raise RuntimeError("ModelRoleUnavailable: 没配模型")

    monkeypatch.setattr(run_node_mod, "_run_node_tool", _raising_run_node)

    ctx, entry = _Ctx(), _authorized_entry()
    assert await pause_driver._execute_authorized_action(ctx, entry) is False
    assert "ModelRoleUnavailable" in entry["action_last_failure"]


@pytest.mark.asyncio
async def test_the_rerun_carries_the_original_task_not_only_the_feedback(monkeypatch):
    """重跑带着原 node_inputs 走 —— 否则 experiment 收到的是一次没有任务的返工。

    这也是上一条失败的**根因**：三个框架键与 experiment / observation /
    writing 的 expected_inputs 零交集，契约检查据此拒绝。
    """
    from core import pause_driver
    import shared.tools.run_node as run_node_mod

    seen: dict = {}

    async def _capturing_run_node(state_, target, *, node_inputs, **kw):
        seen["target"] = target
        seen["inputs"] = dict(node_inputs)
        return {"status": "completed", "run_id": "run_child_1"}

    monkeypatch.setattr(run_node_mod, "_run_node_tool", _capturing_run_node)

    ctx = _Ctx()
    entry = _authorized_entry(producing_node_inputs={
        "experiment_spec": "L=64 Ising，2σ 判据",
        "prereg_artifact_id": "pre_registration__H1",
    })
    assert await pause_driver._execute_authorized_action(ctx, entry) is True

    assert seen["target"] == "experiment"
    assert seen["inputs"]["experiment_spec"] == "L=64 Ising，2σ 判据", (
        "重跑丢了原任务：节点只拿到反馈，定义不出该重做什么"
    )
    assert seen["inputs"]["prereg_artifact_id"] == "pre_registration__H1"
    assert seen["inputs"]["mode"] == "revise"
    assert "L=64 那一档缺收敛判据" in seen["inputs"]["reviewer_feedback"]


@pytest.mark.asyncio
async def test_the_producing_run_registers_its_inputs_on_the_flow_entry():
    """原任务要在**登记 flow entry 那一刻**被记下来，不是派发时去别处捞。

    没有这一条，上面那条只证明"如果有人填了这个键，派发会用它"。
    """
    import inspect

    import shared.tools.run_node as run_node_mod

    src = inspect.getsource(run_node_mod)
    head = src.index('flow_entry = {')
    body = src[head:head + 1500]
    assert '"producing_node_inputs"' in body, (
        "flow entry 没带上这一轮的 node_inputs —— 运行时重跑时就没有任务可带"
    )
