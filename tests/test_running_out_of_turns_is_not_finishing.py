"""「做完了」和「轮次用尽被切断」是两件事，不能记成同一个状态。

## 现场（E2E v23，2026-08-11，空转三轮 ≈ 三小时）

experiment 跑到第 40 轮被 `max_turns` 截断。它自己在产出说明里写得清清楚楚：

    「提交了 6 个 LAMMPS 模拟，全部成功完成。但达到 max_turns=40 截断，
      未做 MSD 分析。14/14 模拟数据都在 jobs/ 目录」

而框架记的是：

    summary.json
      final_status: completed     ← 撒谎
      turns: 40                   ← 事实在这儿，但没人拿它跟上限比
      （没有 stop_reason / truncated 字段）

于是决策层看不出这份交付物是残缺的：

    07:09 决策 retry_reviewer → reviewer 跑 11 分钟 → 完成
    07:22 决策 retry_curator  → curator  跑 45 分钟 → 完成
    08:xx 决策 retry_reviewer → …

一轮约一小时，三轮没有任何进展。菜单里三套动作
（`_NORMAL_ACTIONS` / `_REVIEW_FAILED_ACTIONS` / `_CURATOR_PENDING_ACTIONS`）
都没有"接着把这个节点跑完"，而真正对的那条 —— `revise`（带反馈重跑 producer）
—— 因为决策层不知道被截断，从来没被推荐过。

`core/agent_loop.py` 的注释自己就承认了这个合并：

    "completed"：LLM 自己决定停止（不再调工具）**或达到 max_turns**

## 改法：记事实，不改判决

机器负责"看见"：把结束原因当成一个机械事实记下来
（`stop_reason`，取自框架自己的循环出口，不是模型自述）。

判决仍归决策层 / reviewer —— 但它们现在**看得见**这份交付物是被切断的，
`revise` 因此成为有依据的推荐，而不是在两个 retry 之间掷硬币。

不把"轮次用尽"记成失败：已经跑完的 14 个模拟是真的产出。它只是**没做完**。
"""
from __future__ import annotations

import pytest

from core import agent_loop


def test_the_loop_reports_why_it_stopped() -> None:
    """框架自己的循环出口是机械事实 —— 别让下游从 turns 数字去猜。"""
    assert hasattr(agent_loop, "STOP_FINISHED")
    assert hasattr(agent_loop, "STOP_MAX_TURNS")
    assert agent_loop.STOP_FINISHED != agent_loop.STOP_MAX_TURNS


def test_running_out_of_turns_is_recorded_in_the_summary(tmp_path) -> None:
    """`turns: 40` 不够 —— 没人会拿它跟上限比。要有一个直说的字段。"""
    from core.executor import summarize_stop_reason

    assert summarize_stop_reason(turns=40, max_turns=40) == agent_loop.STOP_MAX_TURNS
    assert summarize_stop_reason(turns=12, max_turns=40) == agent_loop.STOP_FINISHED


def test_a_truncated_run_is_not_silently_completed() -> None:
    """状态不能只有一个 `completed` —— 那正是决策层三小时选错动作的原因。"""
    from core.executor import truncated_by_turn_cap

    assert truncated_by_turn_cap({"turns": 40, "max_turns": 40}) is True
    assert truncated_by_turn_cap({"turns": 9, "max_turns": 40}) is False
    # 缺字段时**不要**猜成"被截断"：把正常完成误判成残缺，会让流程无限 revise
    assert truncated_by_turn_cap({"turns": 40}) is False
    assert truncated_by_turn_cap({}) is False


def test_the_decision_package_shows_it_and_recommends_revise() -> None:
    """决策层看得见 → `revise` 变成有依据的推荐，不再在两个 retry 之间掷硬币。"""
    from shared.tools.library.decision_package import recommended_action_for_truncated

    assert recommended_action_for_truncated(True, "retry_reviewer") == "revise"
    assert recommended_action_for_truncated(True, "retry_curator") == "revise"
    # 没被截断时一个字不动别人的判断
    assert recommended_action_for_truncated(False, "retry_reviewer") == "retry_reviewer"
    assert recommended_action_for_truncated(False, "proceed") == "proceed"


def test_truncation_does_not_become_a_failure_verdict() -> None:
    """已经跑完的 14 个模拟是真产出 —— 它只是**没做完**，不是失败。

    把"没做完"记成失败，等于把真实数据连同状态一起否定掉（今晚下线 QC 判决层
    正是因为这类合并）。
    """
    from core.executor import truncated_by_turn_cap

    summary = {"turns": 40, "max_turns": 40, "artifacts": ["job_submission__x"]}
    assert truncated_by_turn_cap(summary) is True
    assert summary["artifacts"], "产物照旧算数"


def test_the_loop_actually_carries_the_cap_out(monkeypatch) -> None:
    """接线：`LoopResult` 真的带出 `max_turns`。

    第一版我写的是 `getattr(loop_result, "max_turns", 0)` —— 而 `LoopResult`
    根本没这个字段，于是永远取 0，`truncated_by_turn_cap` 永远 False，
    **整条修复是哑的**。「机制存在但没接到路径」是这个项目最常见的缺陷形状，
    新加的机制也得自己受这一问。
    """
    import dataclasses

    fields = {f.name for f in dataclasses.fields(agent_loop.LoopResult)}
    assert "max_turns" in fields, "LoopResult 不带上限出来，下游就只能猜"

    result = agent_loop.LoopResult(final_text="x", turns=40, max_turns=40)
    from core.executor import summarize_stop_reason, truncated_by_turn_cap

    assert summarize_stop_reason(turns=result.turns, max_turns=result.max_turns) \
        == agent_loop.STOP_MAX_TURNS
    assert truncated_by_turn_cap({"turns": result.turns, "max_turns": result.max_turns})


def test_every_loop_exit_carries_it() -> None:
    """循环里每个 `LoopResult(` 构造点都要带 —— 漏一个就是一条静默失效的出口。"""
    import inspect
    import re

    src = inspect.getsource(agent_loop)
    body = src[src.index("for turn in range(1, _max_turns + 1)"):]
    constructions = len(re.findall(r"LoopResult\(", body))
    carried = len(re.findall(r"max_turns=_max_turns", body))
    assert carried == constructions, (
        f"{constructions} 个构造点里只有 {carried} 个带了上限"
    )


def test_the_decision_layer_actually_reads_the_summary(tmp_path, monkeypatch) -> None:
    """接线：`_producer_was_truncated` 真的从 summary.json 读得到。

    上一个函数（`recommended_action_for_truncated`）写完之后没人调 —— 同一个
    陷阱在这个 PR 里已经出现过一次。这条走真实的读盘路径。
    """
    from shared.tools.library import decision_package as dp

    runs = tmp_path / "runs"
    (runs / "run-truncated").mkdir(parents=True)
    (runs / "run-truncated" / "summary.json").write_text(
        '{"turns": 40, "max_turns": 40, "node_type": "experiment"}', encoding="utf-8")
    (runs / "run-finished").mkdir(parents=True)
    (runs / "run-finished" / "summary.json").write_text(
        '{"turns": 9, "max_turns": 40, "node_type": "experiment"}', encoding="utf-8")

    monkeypatch.setattr("core.paths.runs_parent", lambda _pid: runs)

    class _S:
        project_id = "p"

    assert dp._producer_was_truncated(_S(), "run-truncated") is not None
    assert dp._producer_was_truncated(_S(), "run-finished") is None
    assert dp._producer_was_truncated(_S(), "run-missing") is None
    assert dp._producer_was_truncated(_S(), "") is None


def test_the_summary_takes_the_cap_from_the_loop_not_a_literal() -> None:
    """接线的最后一段：summary 里的 `max_turns` 必须来自 loop_result。

    变异测试抓到的漏洞：把它改成字面量 `0` 之后，上面所有用例照旧全绿 ——
    因为它们都在测函数本身，没有一条测"executor 真的把这个值写进 summary"。
    整条链上任何一段断了，修复就是哑的。
    """
    import inspect
    import re

    from core import executor

    src = inspect.getsource(executor)
    m = re.search(r'"max_turns":\s*([^\n,]+)', src)
    assert m, "summary 里根本没写 max_turns"
    assert "loop_result" in m.group(1), (
        f'max_turns 不是从 loop_result 取的：{m.group(1)!r} —— 写死之后整条判据归零'
    )
    m2 = re.search(r'"stop_reason":\s*([^\n]+)', src)
    assert m2 and "summarize_stop_reason" in m2.group(1), "stop_reason 必须现算"
