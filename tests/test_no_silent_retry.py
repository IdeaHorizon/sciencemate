"""两条契约不变量的回归（E2E-4 溯源门禁误诊事故的类级修法）。

  ① 机械拒绝的理由必须原样到达要做决定的那一方。
  ② 没有增量，不许重试。

现场：data 节点被 data_provenance_declared 拦下，门禁生成了 20 行错误文案
（具体文件、mtime 证据、两条合法出路），但 run_node 返回给 orchestrator 的只有
check **名字**。orchestrator 猜成"shell 被限只读"，换工具去读同一批旧文件，
连续两轮输出一字不差，最后 stall 熔断止血 —— 熔断只是把错误卡住，没有让生产
路线成功。

这两条不变量同时是 PR#196（判决理由二次截断）、"null" 活锁（理由是垃圾值）的
类级收敛：那些补丁都是同一条契约的不同违背。
"""
from __future__ import annotations

import json

import chat as chat_mod
from core.bootstrap import bootstrap
from core.state import State

bootstrap()

PROVENANCE_REASON = (
    "⛔ 本 run 的产物依赖了 2 个**比本 run 还老的外部数据文件**，但没有任何产物"
    "声明它们的来源。合规做法二选一：① 真的要复用 → 在产物 metadata 里写 "
    "`reused_inputs`；② 本该自己跑 → 就去跑。"
)


def _failed_data_run(base, name, *, project_id):
    d = base / name
    (d / "artifacts").mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "node_type": "data", "project_id": project_id, "status": "incomplete",
        "missing_required_outputs": ["dataset"],
        "quality_check_results": [
            {"name": "data_provenance_declared", "passed": False,
             "mechanical": True, "reasoning": PROVENANCE_REASON},
            {"name": "some_other_check", "passed": True, "reasoning": "ok"},
        ],
        "state_dir": str(d),
        "artifacts": [],
    }), encoding="utf-8")
    return d


# ── ① 理由送达：已由写入面结构性保证（2026-08-22 QC 层删除）────────────
# 机械拒绝现在发生在工具调用当场，完整理由直接在工具返回里到达模型 ——
# 不再经过 run 间账本的转运，_failed_check_reasons 及其接线随层删除。

# ── ② 没有增量不许重试 ──────────────────────────────────────────────────────

def _continuous_state(tmp_path):
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    state.hook_state.update(continuous_loop=True, continuous_phase="running")
    return state


REPLY = ("诊断停滞根因：data 节点 incomplete，改用 read_file 直接读轨迹文件。"
         "\nCONTINUOUS_STATUS: continue")


def test_verbatim_repeat_injects_mechanical_delta_then_stops(tmp_path):
    """一字不差重复：第一次注入机械 delta，仍重复 → 立刻停。

    E2E-4 现场就是这个序列，只不过当时没有 delta 注入，模型猜错后原地转到
    stall 熔断。熔断的正确定位是正常运行永不触发 —— delta 注入才是生产路线。
    """
    state = _continuous_state(tmp_path)
    _failed_data_run(state.root.parent, "1800000002-d",
                     project_id=state.project_id)

    # 第 1 轮：正常续轮（还没有重复）
    p1, _ = chat_mod._continuous_followup(state, REPLY, reason="turn_finished")
    assert p1 is not None and "一字不差" not in p1

    # 第 2 轮：一字不差 → 注入机械 delta（门禁的完整理由 + 出路）
    p2, _ = chat_mod._continuous_followup(state, REPLY, reason="turn_finished")
    assert p2 is not None
    assert "一字不差" in p2
    assert "dataset" in p2  # 机械事实 = 缺的产出（QC 判定理由已随层删除）
    # 「合法出路」现在由写入面工具在拒绝当场返回（QC 转运账本已删 2026-08-22）
    assert "1800000002-d" in p2, "delta 必须指名是哪次失败"

    # 第 3 轮：注入过 delta 仍一字不差 → 立刻停，不等 stall 爬到 12
    p3, _ = chat_mod._continuous_followup(state, REPLY, reason="turn_finished")
    assert p3 is None
    assert state.hook_state["continuous_phase"] == "aborted"
    tr = state.transcript_path.read_text(encoding="utf-8")
    assert "no_delta_repeat" in tr
    assert "continuous_delta_injected" in tr


def test_changing_replies_never_trigger_delta_or_stop(tmp_path):
    """输出在变 = 有增量，不许误伤。"""
    state = _continuous_state(tmp_path)
    _failed_data_run(state.root.parent, "1800000003-d",
                     project_id=state.project_id)
    # 注意不能只差一个数字 —— 那正是"自增计数"循环模式，按规则就该被判重复
    # （第一版测试就是这么写的，数词归一化上线后被自己击中）。
    texts = ["启动 literature 检索", "评审 survey 产物",
             "整理 KB proposal", "派发 hypothesis 节点"]
    for txt in texts:
        p, _ = chat_mod._continuous_followup(
            state, f"{txt}\nCONTINUOUS_STATUS: continue",
            reason="turn_finished")
        assert p is not None and "一字不差" not in p
    assert state.hook_state["continuous_phase"] == "running"


def test_verbatim_with_progress_is_not_punished(tmp_path):
    """文本相同但有真实进展（fingerprint 变了）→ 不触发。

    进度指纹在变说明世界在动，重复的叙述文本无害；不变量②只打击
    "无进展 + 无变化"的组合。
    """
    state = _continuous_state(tmp_path)
    p1, _ = chat_mod._continuous_followup(state, REPLY, reason="turn_finished")
    # 制造进展：新的兄弟 run 落盘（进度指纹含 summaries 数量）
    _failed_data_run(state.root.parent, "1800000004-d",
                     project_id=state.project_id)
    p2, _ = chat_mod._continuous_followup(state, REPLY, reason="turn_finished")
    assert p2 is not None and "一字不差" not in p2


def test_verbatim_repeat_without_any_delta_stops_immediately(tmp_path):
    """没有失败 run 可注入 = 没有任何机械 delta → 第二次重复直接停。

    重试 N 次也是同一句话，不需要等熔断线。
    """
    state = _continuous_state(tmp_path)
    p1, _ = chat_mod._continuous_followup(state, REPLY, reason="turn_finished")
    assert p1 is not None
    p2, _ = chat_mod._continuous_followup(state, REPLY, reason="turn_finished")
    assert p2 is None
    assert state.hook_state["continuous_phase"] == "aborted"


def test_delta_helper_reads_authoritative_history(tmp_path):
    state = _continuous_state(tmp_path)
    assert chat_mod._latest_failed_producing_delta(state) is None
    _failed_data_run(state.root.parent, "1800000005-d",
                     project_id=state.project_id)
    d = chat_mod._latest_failed_producing_delta(state)
    assert d["node_type"] == "data" and d["missing"] == ["dataset"]


def test_incrementing_counter_replies_count_as_verbatim(tmp_path):
    """E2E-4 二次实测：重复文本带自增计数（"连续十四轮…"→"十五轮…"），每轮差
    一个字，逐字 hash 形同虚设，烧了 23 轮。数词归一化后必须判为重复。"""
    state = _continuous_state(tmp_path)
    _failed_data_run(state.root.parent, "1800000006-d",
                     project_id=state.project_id)
    p1, _ = chat_mod._continuous_followup(
        state, "我连续十四轮写了参数描述但没有实际调用工具。\nCONTINUOUS_STATUS: continue",
        reason="turn_finished")
    assert p1 is not None and "一字不差" not in p1
    p2, _ = chat_mod._continuous_followup(
        state, "我连续十五轮写了参数描述但没有实际调用工具。\nCONTINUOUS_STATUS: continue",
        reason="turn_finished")
    assert p2 is not None and "一字不差" in p2, "只差一个数词 = 同一句话"
    p3, _ = chat_mod._continuous_followup(
        state, "我连续十六轮写了参数描述但没有实际调用工具。\nCONTINUOUS_STATUS: continue",
        reason="turn_finished")
    assert p3 is None and state.hook_state["continuous_phase"] == "aborted"


def test_cancelled_summary_carries_appeals_and_missing(tmp_path):
    """cancel 路径不许把账丢了（E2E-4 实测）。

    data 节点被自己的 terminal guard 取消 3 次，每次都调了
    request_upstream_rework 申诉 —— 但 cancel 路径的 summary 硬编码
    missing=[] 且不带申诉：申诉永远浮不出来，且 3 次取消在派发拦截统计里
    完全隐形。cancel ≠ 什么都没发生。
    """
    import asyncio

    from core.executor import execute_node
    from core.harness import NodeHarness

    class _CancelLoop:
        status = "cancelled"
        cancel_meta = {"reason": "terminal guard"}
        turns = 4
        tool_calls = []
        final_text = "blocked"
        pause_event = None

    # 注意不能用 data —— 它有 custom agent_loop，monkeypatch 会被绕过
    harness = NodeHarness(node_type="postprocess", system_prompt="collect",
                          required_outputs=["dataset"])

    async def _run():
        import core.executor as ex
        orig = ex.run_loop

        async def _fake_loop(harness_, state_, messages_, llm_, **k):
            # 申诉发生在 loop 里 —— 塞进 hook_state 模拟节点真调过工具
            st = state_
            st.hook_state["upstream_rework_requests"] = [{
                "requested_by_node": "postprocess", "upstream_node": "_orchestrator",
                "missing": "approved preprocessing plan",
                "acceptance": "plan containing execute_preprocessing_python",
            }]
            return _CancelLoop()

        ex.run_loop = _fake_loop
        try:
            return await execute_node(
                "postprocess", state_dir=tmp_path / "runs",
                project_id="p_cancel", harness_override=harness, llm=object())
        finally:
            ex.run_loop = orig

    summary = asyncio.run(_run())
    assert summary["status"] == "cancelled"
    assert summary["missing_required_outputs"] == ["dataset"], \
        "取消时没产出的必需产物必须记账，否则派发拦截看不见"
    appeals = summary["upstream_rework_requests"]
    assert appeals and appeals[0]["upstream_node"] == "_orchestrator", \
        "节点的申诉必须活着进 summary，否则永远浮不到 orchestrator 面前"


# ── provider 断流要走得到那道机械重试（issue #480）──────────────────────────
#
# 现场：ReadError / RemoteProtocolError 打穿 core/llm 的重试预算后，
# execute_node 写完 summary 就 re-raise。异常沿工具调用往上冒，而这一层的机械
# 重试读的是 summary 的 failure_category —— 这条路上压根没有 summary 可读，于是
# 重试**从来没被触发过**。零产出的 literature / hypothesis 就这么被一次接口
# 抖动打死，orchestrator 只看到一段 traceback。

import httpx
import pytest

from core.state import State


def _state(tmp_path):
    root = tmp_path / "runs" / "parent"
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    return State(run_id="parent", node_type="_orchestrator", root=root)


@pytest.mark.asyncio
async def test_a_provider_outage_gets_retried_instead_of_crashing(tmp_path):
    from shared.tools.run_node import _execute_with_infra_retry_inner

    calls = {"n": 0}

    async def _flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("stream truncated")
        return {"status": "completed", "run_id": "child", "produced_artifact_types": ["x"]}

    summary = await _execute_with_infra_retry_inner(
        _state(tmp_path), "literature", {}, _flaky)

    assert calls["n"] == 2, "provider 抖了一下就判死，机械重试根本没接到这条路"
    assert summary["status"] == "completed"


@pytest.mark.asyncio
async def test_a_node_side_exception_is_not_retried(tmp_path):
    """只兜 provider 故障。节点自己的 bug 重试一遍还是同样的 bug。"""
    from shared.tools.run_node import _execute_with_infra_retry_inner

    calls = {"n": 0}

    async def _broken(**kwargs):
        calls["n"] += 1
        raise ValueError("节点自己的 bug")

    with pytest.raises(ValueError):
        await _execute_with_infra_retry_inner(_state(tmp_path), "literature", {}, _broken)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_persistent_outage_still_surfaces_the_original_exception(tmp_path):
    """一直挂着就照旧抛原异常 —— 不要把故障吞成"跑完了"。"""
    from shared.tools.run_node import _execute_with_infra_retry_inner

    calls = {"n": 0}

    async def _always_down(**kwargs):
        calls["n"] += 1
        raise httpx.RemoteProtocolError("server disconnected")

    with pytest.raises(httpx.RemoteProtocolError):
        await _execute_with_infra_retry_inner(_state(tmp_path), "literature", {}, _always_down)
    assert calls["n"] > 1, "一次都没重试"


@pytest.mark.asyncio
async def test_a_child_that_already_produced_something_is_not_rerun(tmp_path, monkeypatch):
    """已经产出了东西的 run 不重跑：重跑既浪费又可能覆盖成果。

    判据取子 run **自己写下的** summary（executor 在 re-raise 前已落盘），
    不是凭空假设"什么都没产出"。
    """
    import shared.tools.run_node as rn

    monkeypatch.setattr(rn, "_crashed_child_summary", lambda *a, **k: {
        "status": "error",
        "failure_category": "provider_unavailable",
        "produced_artifact_types": ["survey_report"],
    })
    calls = {"n": 0}

    async def _down_after_work(**kwargs):
        calls["n"] += 1
        raise httpx.ReadTimeout("read timed out")

    summary = await rn._execute_with_infra_retry_inner(
        _state(tmp_path), "literature", {}, _down_after_work)

    assert calls["n"] == 1, "这一轮已经产出了 survey_report，不该原样重跑"
    assert summary["failure_category"] == "provider_unavailable"
