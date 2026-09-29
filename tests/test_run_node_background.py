"""run_node background 模式（R1 异步基座 v1）。

科研任务时间常数（小时~天）≠ 对话（秒）：background=true 让 child 进程内后台
跑，对话立即返回；完成/暂停/失败经 ①_CHILD_EVENT_SINK（用户即时看到）
②parent hook_state['injected_messages']（模型下一轮看到）双路回报 ——
维持"用户可见 = 模型可见"不变量。不联网（execute_node 打桩）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import bootstrap
from core.state import State

bootstrap()

from shared.tools import run_node as rn   # noqa: E402


def _parent(tmp_path: Path) -> State:
    s = State.new(node_type="_orchestrator", base_dir=tmp_path,
                    project_id="p_bg")
    s.hook_state["_callable_nodes"] = ["*"]
    return s


def _summary(run_id="run_bg1", status="completed", state_dir="", node_type="_curator"):
    return {
        "run_id": run_id, "status": status, "node_type": node_type,
        "turns": 3, "tokens_used": 100, "artifacts": [],
        "missing_required_outputs": [], "state_dir": state_dir,
        "final_text_preview": "done",
    }


@pytest.fixture(autouse=True)
def _clean_sink():
    yield
    rn.set_child_event_sink(None)
    rn._BG_PAUSED_CONTINUATIONS.clear()


@pytest.mark.asyncio
async def test_background_returns_immediately_then_reports(tmp_path, monkeypatch):
    parent = _parent(tmp_path)
    events: list[dict] = []
    rn.set_child_event_sink(events.append)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_execute(**kwargs):
        started.set()
        await release.wait()          # 卡住模拟长任务
        sd = tmp_path / "child_run"; (sd / "artifacts").mkdir(parents=True, exist_ok=True)
        return _summary(state_dir=str(sd))

    monkeypatch.setattr("core.executor.execute_node", slow_execute)

    out = await rn._run_node_tool(parent, node_type="_curator", user_note="测试派发",
                                    node_inputs={"mode": "dreaming"},
                                    background=True)
    # 立即返回，child 还没跑完
    assert out["status"] == "started_background"
    await asyncio.wait_for(started.wait(), 2)
    assert events == []                      # 还没完成，无事件

    release.set()                            # 放行
    await asyncio.sleep(0.05)
    assert [e["kind"] for e in events] == ["completed"]
    # 双路之二：注入给模型
    inj = parent.hook_state.get("injected_messages") or []
    assert any("后台子节点回报" in m["content"] for m in inj)


@pytest.mark.asyncio
async def test_background_pause_notifies_and_answer_routes(tmp_path, monkeypatch):
    parent = _parent(tmp_path)
    events: list[dict] = []
    rn.set_child_event_sink(events.append)

    async def pausing_execute(**kwargs):
        return {**_summary(run_id="run_paused1", status="paused"),
                "pause_event": {"question": "NHC chain 用 3 还是 5？"}}

    monkeypatch.setattr("core.executor.execute_node", pausing_execute)
    out = await rn._run_node_tool(parent, node_type="_curator", user_note="测试派发",
                                    node_inputs={}, background=True)
    assert out["status"] == "started_background"
    await asyncio.sleep(0.05)

    assert [e["kind"] for e in events] == ["paused"]
    assert "NHC" in events[0]["question"]
    assert "run_paused1" in rn._BG_PAUSED_CONTINUATIONS       # 续跑上下文已登记
    inj = parent.hook_state.get("injected_messages") or []
    assert any("/answer" in m["content"] for m in inj)


@pytest.mark.asyncio
async def test_background_crash_reported_not_swallowed(tmp_path, monkeypatch):
    parent = _parent(tmp_path)
    events: list[dict] = []
    rn.set_child_event_sink(events.append)

    async def crashing_execute(**kwargs):
        raise RuntimeError("LLM 端点炸了")

    monkeypatch.setattr("core.executor.execute_node", crashing_execute)
    await rn._run_node_tool(parent, node_type="_curator", user_note="测试派发",
                              node_inputs={}, background=True)
    await asyncio.sleep(0.05)
    assert [e["kind"] for e in events] == ["failed"]
    assert "LLM 端点炸了" in events[0]["error"]
    inj = parent.hook_state.get("injected_messages") or []
    assert any("崩溃" in m["content"] for m in inj)


@pytest.mark.asyncio
async def test_foreground_default_unchanged(tmp_path, monkeypatch):
    """不传 background → 老同步行为原样（回归保护）。"""
    parent = _parent(tmp_path)

    async def instant_execute(**kwargs):
        sd = tmp_path / "child_fg"; (sd / "artifacts").mkdir(parents=True, exist_ok=True)
        return _summary(run_id="run_fg", state_dir=str(sd))

    monkeypatch.setattr("core.executor.execute_node", instant_execute)
    out = await rn._run_node_tool(parent, node_type="_curator", user_note="测试派发", node_inputs={})
    assert out["status"] == "success"
    assert out["child_run_id"] == "run_fg"
