"""同一个 run 同一时刻只能有一个 loop（#1164）。

现场（2026-09-28，SGD checkpoint 任务）：child 的审批被框架自动作答续上、继续
在跑；父那张旧审批卡还挂着。用户回了一句「允许」，待命旁路的模型调起
run_node —— 那个工具根本没展示给它 —— 恢复扫描按「有 run_start、没 run_end」
把**还在跑的** child 判成被打断，自动续上。同一个 run_id 上两个 loop 重叠两分半：
两个 loop_start、两次 finish、两个 run_end。

三道各管一处：
  1. 扫描：还有执行者（本进程 active / paused / 有 driver）的 run 不是被打断；
     显式指名续它 → resume_target_still_running。
  2. executor：reopen 前最后核一次，还有执行者就拒（不退回新开）。
  3. 待命旁路：只执行展示过的工具；参数解析不了不当成「没给」去执行。
真被打断（进程死了、登记全空）的 run 照旧续得上。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMResponse
from core.pause import ActiveRunInfo, claim_driver, clear_all, register_active
from core.state import State

bootstrap()

from shared.tools import run_node as rn   # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_all()
    yield
    clear_all()


def _orchestrator(tmp_path: Path) -> State:
    s = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs")
    s.hook_state["_callable_nodes"] = ["*"]
    return s


def _unfinished_child(parent: State, run_id: str = "run_sgd_child",
                      node_type: str = "_curator") -> Path:
    """盘上一条有 run_start、没 run_end、有 checkpoint 的 child —— 两种情况都长这样：
    真被打断的，和还在跑的。"""
    d = parent.root.parent / run_id
    d.mkdir(parents=True, exist_ok=True)
    with (d / "transcript.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"event": "run_start", "node_type": node_type,
                            "parent_run_id": parent.run_id}) + "\n")
        f.write(json.dumps({"event": "tool_call", "name": "x"}) + "\n")
    (d / "messages_checkpoint.json").write_text(json.dumps({
        "turn": 3, "written_at": "2026-09-28T02:05:44Z",
        "messages": [{"role": "system", "content": "s"},
                     {"role": "assistant", "content": "第 6 阶段跑完了"}],
    }), encoding="utf-8")
    return d


def _mark_live(run_id: str, base: Path, node_type: str = "_curator") -> None:
    live = State.reopen(node_type=node_type, base_dir=base, run_id=run_id)
    register_active(ActiveRunInfo(run_id=run_id, node_type=node_type, state=live))


# ── 1. 扫描 ──────────────────────────────────────────────────────────────

def test_a_running_child_is_not_offered_for_auto_resume(tmp_path):
    parent = _orchestrator(tmp_path)
    _unfinished_child(parent)
    # 同一份盘面：没人在跑 → 是被打断，续它
    assert rn._resumable_run_for(parent, "_curator")["run_id"] == "run_sgd_child"

    _mark_live("run_sgd_child", parent.root.parent)
    # 还有执行者 → 不是被打断，不续、不简报
    assert rn._resumable_run_for(parent, "_curator") is None
    assert rn.brief_interrupted_child_runs(parent) == 0


def test_a_child_waiting_on_an_answer_is_not_interrupted_either(tmp_path):
    parent = _orchestrator(tmp_path)
    _unfinished_child(parent)
    claim_driver("run_sgd_child")      # 有人在等它的答复
    assert rn._resumable_run_for(parent, "_curator") is None


def test_after_the_process_dies_the_same_run_resumes(tmp_path):
    """登记随进程消失 —— 真被打断的 run 仍然判得出来、续得上。"""
    parent = _orchestrator(tmp_path)
    _unfinished_child(parent)
    _mark_live("run_sgd_child", parent.root.parent)
    clear_all()                         # = worker 进程死了
    assert rn._resumable_run_for(parent, "_curator")["run_id"] == "run_sgd_child"


@pytest.mark.asyncio
async def test_naming_a_running_child_to_resume_is_refused(tmp_path, monkeypatch):
    parent = _orchestrator(tmp_path)
    _unfinished_child(parent)
    _mark_live("run_sgd_child", parent.root.parent)
    calls: list[dict] = []

    async def fake_execute(**kwargs):
        calls.append(kwargs)
        raise AssertionError("不该起第二个 loop")

    monkeypatch.setattr("core.executor.execute_node", fake_execute)
    out = await rn._run_node_tool(parent, node_type="_curator", user_note="续上",
                                  resume_run_id="run_sgd_child")
    assert out["status"] == "error"
    assert out["error_code"] == "resume_target_still_running"
    assert calls == []


# ── 2. executor ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_executor_will_not_reopen_a_run_that_has_an_executor(tmp_path, monkeypatch):
    from core.executor import RunStillLiveError, execute_node

    parent = _orchestrator(tmp_path)
    _unfinished_child(parent)
    _mark_live("run_sgd_child", parent.root.parent)
    loops: list[str] = []

    async def fake_run_loop(harness, state, messages, llm):
        loops.append(state.run_id)
        raise AssertionError("不该进 loop")

    monkeypatch.setattr("core.executor.run_loop", fake_run_loop)
    with pytest.raises(RunStillLiveError):
        await execute_node("_curator", state_dir=parent.root.parent, project_id=None,
                           resume_run_id="run_sgd_child")
    assert loops == []
    # 盘上那条 run 没被追加任何东西（没 reopen、没写第二个 run_start）
    events = [json.loads(x)["event"] for x in
              (parent.root.parent / "run_sgd_child" / "transcript.jsonl")
              .read_text(encoding="utf-8").splitlines() if x.strip()]
    assert events.count("run_start") == 1


# ── 3. 待命旁路 ──────────────────────────────────────────────────────────

def _load_chat_module():
    spec = importlib.util.spec_from_file_location(
        "_chat_one_loop", Path(__file__).parent.parent / "chat.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, **kw):
        if not self.responses:
            return LLMResponse(content="好的。", tool_calls=[], finish_reason="stop", usage={})
        return self.responses.pop(0)


def _call(name: str, arguments: str, cid: str = "tc") -> dict:
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": arguments}}


@pytest.mark.asyncio
async def test_standby_does_not_execute_a_tool_it_was_not_offered(tmp_path, monkeypatch):
    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    child = State.new(node_type="_curator", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(run_id=child.run_id, node_type="_curator", state=child))
    executed: list[str] = []

    async def recording_execute(name, state, **kw):
        executed.append(name)
        return {"status": "success"}

    monkeypatch.setattr("core.tool_registry.execute", recording_execute)
    llm = _ScriptedLLM([LLMResponse(
        content=None, finish_reason="tool_calls", usage={},
        tool_calls=[_call("run_node", json.dumps(
            {"node_type": "_curator", "user_note": "允许"}))])])
    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                          tools=["runtime_control", "run_node"],
                          max_turns=2, max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, llm, "允许")
    assert "run_node" not in executed


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["{\"action\": \"progress\"", "[1, 2]"])
async def test_standby_does_not_run_a_call_whose_arguments_do_not_parse(
        tmp_path, monkeypatch, raw):
    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    child = State.new(node_type="_curator", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(run_id=child.run_id, node_type="_curator", state=child))
    executed: list[tuple[str, dict]] = []

    async def recording_execute(name, state, **kw):
        executed.append((name, kw))
        return {"status": "success"}

    monkeypatch.setattr("core.tool_registry.execute", recording_execute)
    llm = _ScriptedLLM([LLMResponse(
        content=None, finish_reason="tool_calls", usage={},
        tool_calls=[_call("runtime_control", raw)])])
    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                          tools=["runtime_control"], max_turns=2, max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, llm, "跑到哪了")
    assert executed == []
