"""incomplete run 的失败分类（issue #106）。

背景：writing 节点 Step 5/6 手动测试中，部分 OpenAI-compatible provider
偶发返回 DSML 协议残片文本（`</｜DSML｜tool_calls>` 等），但
finish_reason=stop 且 tool_calls=[]。agent_loop 把这当模型自然结束，run
进 incomplete —— 这是 provider tool-call 协议兼容性故障，不是节点
prompt/写作质量问题，混在一起测试记录和排查方向都会跑偏。

只测分类结果，不测行为改变：这个改动不解析 DSML 残片、不加 provider 补丁，
只是给 incomplete run 的 summary 打一个 failure_category 标记。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.agent_loop import LoopResult
from core.executor import _classify_incomplete_failure, finalize_run
from core.harness import NodeHarness
from core.llm import LLMClient
from core.pause import clear_all
from core.state import State


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield
    clear_all()


_DSML_FRAGMENT = (
    "</parameter>\n</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜tool_calls>"
)


# ── _classify_incomplete_failure：纯函数分类逻辑 ────────────────────────────

def test_classify_dsml_fragment_with_zero_tool_calls():
    lr = LoopResult(final_text=_DSML_FRAGMENT, turns=6, tool_calls=[])
    assert _classify_incomplete_failure(lr) == "provider_tool_call_protocol_error"


def test_classify_normal_blocked_run_not_misclassified():
    """普通材料不足 blocked run（没有 DSML 残片）不被误分类。"""
    lr = LoopResult(
        final_text="缺少必要的 upstream 材料，无法继续，请补充 xxx 数据后重跑。",
        turns=2, tool_calls=[],
    )
    assert _classify_incomplete_failure(lr) is None


def test_classify_run_with_tool_calls_not_misclassified():
    """本 run 至少成功调过工具（不是协议层完全失效）→ 即使文本里出现 DSML 残片
    也不分类为协议故障（比如某个 tool_call 的 arguments 里恰好包含这个子串）。"""
    lr = LoopResult(
        final_text=_DSML_FRAGMENT, turns=3,
        tool_calls=[{"id": "c1", "function": {"name": "save_artifact"}}],
    )
    assert _classify_incomplete_failure(lr) is None


def test_classify_empty_text_not_misclassified():
    lr = LoopResult(final_text="", turns=1, tool_calls=[])
    assert _classify_incomplete_failure(lr) is None


# ── finalize_run 集成：failure_category 写进 summary.json ──────────────────

@pytest.mark.asyncio
async def test_finalize_run_incomplete_dsml_writes_failure_category(tmp_path: Path):
    state = State.new(node_type="writing", base_dir=tmp_path)
    harness = NodeHarness(
        node_type="writing", system_prompt="t", tools=[], max_turns=5,
        required_outputs=["manuscript"],   # 没产出 → missing 非空 → incomplete
    )
    loop_result = LoopResult(final_text=_DSML_FRAGMENT, turns=6, tool_calls=[])
    summary = await finalize_run(state, harness, loop_result, LLMClient(
        api_key="k", model="m", base_url="http://x"))
    assert summary["status"] == "incomplete"
    assert summary["failure_category"] == "provider_tool_call_protocol_error"


@pytest.mark.asyncio
async def test_finalize_run_normal_incomplete_no_failure_category(tmp_path: Path):
    """普通缺产出的 incomplete（不是 DSML 协议故障）→ failure_category 为 None。"""
    state = State.new(node_type="writing", base_dir=tmp_path)
    harness = NodeHarness(
        node_type="writing", system_prompt="t", tools=[], max_turns=5,
        required_outputs=["manuscript"],
    )
    loop_result = LoopResult(final_text="材料不足，无法完成写作。", turns=2, tool_calls=[])
    summary = await finalize_run(state, harness, loop_result, LLMClient(
        api_key="k", model="m", base_url="http://x"))
    assert summary["status"] == "incomplete"
    assert summary["failure_category"] is None


@pytest.mark.asyncio
async def test_finalize_run_completed_no_failure_category(tmp_path: Path):
    """成功 run（required outputs 已满足）→ failure_category 为 None，即使 tool_calls 恰好为空
    （比如节点无 required_outputs 时，没工具调用也能 completed）。"""
    state = State.new(node_type="writing", base_dir=tmp_path)
    harness = NodeHarness(
        node_type="writing", system_prompt="t", tools=[], max_turns=5,
        required_outputs=[],
    )
    loop_result = LoopResult(final_text="done, nothing to do here.", turns=1, tool_calls=[])
    summary = await finalize_run(state, harness, loop_result, LLMClient(
        api_key="k", model="m", base_url="http://x"))
    assert summary["status"] == "completed"
    assert summary["failure_category"] is None
