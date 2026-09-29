"""issue #221：orchestrator 把未完成的任务误报成 completed。

实测背景（jicq E2E，基线 4a484ee）：编排器依次派了 literature → hypothesis →
experiment → …，其中关键下游节点 `experiment`（run `1785307479-d15500`）明确
`status=incomplete`、最终交付物没齐，而 **orchestrator 自己 run 的 summary.json
写的是 `completed`**。平台汇总只读这个 status → 用户看到"任务已完成"；自动化
系统据此停止重试 / 提前通知完成。

根因是判据覆盖面：`final_status` 只看本 run 自己的 `required_output_artifact_types`
和 quality_checks，而 `_orchestrator` 两者都是空 —— 协调者不自己产 artifact，
于是"进程正常退出"就等于 completed。

本文件测的是补上的那条协调者专属判据（core/executor.py
`compute_orchestration_closure`）：

  ① 未闭环时不许 completed（3 类机械信号：flow 账本 / TaskList / 子 run 状态）
  ② **不能误杀**：纯对话轮（没编排过 producing 节点）永远不降级 —— 这是最容易
     做错的地方，orchestrator 是长驻节点，chat.py 每解开一次 pause 链就会重写
     一次它的 summary.json，"还有 pending task"在那种时刻是正常中间态
  ③ summary 里能看到**具体**什么没闭环，不是一个布尔
  ④ 判据自身失败时不吞不炸

全部离线：手搓 state.hook_state / transcript 事件 / 假 summary.json / 临时
TaskList，不调 LLM、不联网（orchestrator 是 `_` 开头的系统节点，
_data_provenance_check 与 run_end episode hook 都自动 skip）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agent_loop import LoopResult
from core.executor import (
    FAILURE_CATEGORY_ORCHESTRATION_NOT_CLOSED,
    ORCHESTRATION_CLOSURE_KEY,
    compute_orchestration_closure,
    finalize_run,
)
from core.harness import NodeHarness
from core.pause import clear_all
from core.state import State
from core.tasks import TaskList

PROJECT = "p_221"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    clear_all()


# ── 构件 ────────────────────────────────────────────────────────────────────

def _orch_harness() -> NodeHarness:
    """与 nodes/_orchestrator/harness.yaml 一致的关键形状：两个门禁都是空的。"""
    h = NodeHarness(node_type="_orchestrator")
    h.required_outputs = []
    return h


def _orch_state(tmp_path: Path, project_id: str | None = PROJECT) -> State:
    return State.new(node_type="_orchestrator", base_dir=tmp_path,
                     project_id=project_id)


def _child_run_on_disk(base_dir: Path, run_id: str, node_type: str,
                       status: str, *, project_id: str | None = PROJECT,
                       missing: list[str] | None = None,
) -> None:
    """在兄弟目录里落一份子 run 的 summary.json（run_history 的事实来源）。"""
    d = base_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "run_id": run_id, "node_type": node_type, "project_id": project_id,
        "status": status,
        "missing_required_outputs": missing or [],
        "quality_check_results": [],
        "artifacts": [],
    }, ensure_ascii=False), encoding="utf-8")


def _record_child(state: State, node_type: str, run_id: str,
                  status: str | None) -> None:
    """模拟 run_node 工具在父 transcript 上的记账（start + 终态）。"""
    state.append_transcript("subagent_call_start", child_node_type=node_type,
                            child_depth=1)
    if status == "paused":
        state.append_transcript("subagent_call_paused",
                                child_node_type=node_type,
                                child_run_id=run_id, pause_question="?")
    else:
        state.append_transcript("subagent_call_end", child_node_type=node_type,
                                child_run_id=run_id, child_status=status,
                                child_turns=3)


def _flow_entry(node: str = "hypothesis", run_id: str = "1785307000-aaa111",
                **overrides) -> dict:
    entry = {
        "producing_node": node,
        "producing_run_id": run_id,
        "task_id": None,
        "artifact_ids": ["a1"],
        "review_state": "done",
        "curator_state": "pending",
        "decision_state": "pending",
    }
    entry.update(overrides)
    return entry


def _tasks(state: State) -> TaskList:
    assert state.project_root is not None
    return TaskList(state.project_root / "tasks")


async def _finalize(state: State, harness: NodeHarness | None = None) -> dict:
    lr = LoopResult(final_text="已按计划推进。", turns=4,
                    tool_calls=[{"id": "c1",
                                 "function": {"name": "run_node"}}])
    return await finalize_run(state, harness or _orch_harness(), lr, llm=None)


# ══ ① 未闭环时不许 completed ═════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_incomplete_child_producing_run_blocks_completed(tmp_path: Path):
    """#221 主复现：experiment 子 run incomplete、交付物没齐 → 协调者不得 completed。"""
    state = _orch_state(tmp_path)
    _child_run_on_disk(tmp_path, "1785307479-d15500", "experiment",
                       "incomplete", missing=["experiment_log"])
    _record_child(state, "experiment", "1785307479-d15500", "incomplete")

    summary = await _finalize(state)

    assert summary["status"] == "incomplete"
    # incomplete 但归因字段全空 = 把排查成本推回人工翻日志，必须有明确类别
    assert summary["failure_category"] == FAILURE_CATEGORY_ORCHESTRATION_NOT_CLOSED
    closure = summary[ORCHESTRATION_CLOSURE_KEY]
    assert closure["closed"] is False
    assert closure["downgrades_status"] is True
    # ③ 具体到哪个节点、哪个 run、缺什么 —— 不是一个布尔
    child = [i for i in closure["open_items"] if i["kind"] == "child_run"]
    assert len(child) == 1
    assert child[0]["node_type"] == "experiment"
    assert child[0]["run_id"] == "1785307479-d15500"
    assert child[0]["missing_required_outputs"] == ["experiment_log"]


@pytest.mark.asyncio
async def test_summary_json_on_disk_carries_the_open_items(tmp_path: Path):
    """平台汇总读的是磁盘上的 summary.json —— 账必须落到那里，不只在内存里。"""
    state = _orch_state(tmp_path)
    _child_run_on_disk(tmp_path, "1785307479-d15500", "experiment",
                       "incomplete", missing=["experiment_log"])
    _record_child(state, "experiment", "1785307479-d15500", "incomplete")
    await _finalize(state)

    on_disk = json.loads(state.summary_path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "incomplete"
    assert on_disk[ORCHESTRATION_CLOSURE_KEY]["open_item_count"] >= 1
    assert "experiment" in on_disk[ORCHESTRATION_CLOSURE_KEY]["user_facing_note"]


@pytest.mark.asyncio
async def test_error_child_run_blocks_completed(tmp_path: Path):
    state = _orch_state(tmp_path)
    _child_run_on_disk(tmp_path, "1785307480-e00001", "writing", "error")
    _record_child(state, "writing", "1785307480-e00001", "error")
    summary = await _finalize(state)
    assert summary["status"] == "incomplete"
    assert [i["status"] for i in summary[ORCHESTRATION_CLOSURE_KEY]["open_items"]
            if i["kind"] == "child_run"] == ["error"]


@pytest.mark.asyncio
async def test_open_post_node_flow_blocks_completed(tmp_path: Path):
    """flow 账本条目还在 = 3 步没走完（条目只在 decision 被机械记账后出列）。

    flow 条目本身就是"本 run 编排过 producing 工作"的证据：它只可能由某个
    producing 子 run 成功后登记。所以不需要另有子 run 事件也该降级。
    """
    state = _orch_state(tmp_path)
    state.hook_state["pending_post_node_flow"] = [_flow_entry()]
    summary = await _finalize(state)

    assert summary["status"] == "incomplete"
    flows = [i for i in summary[ORCHESTRATION_CLOSURE_KEY]["open_items"]
             if i["kind"] == "post_node_flow"]
    assert len(flows) == 1
    assert flows[0]["producing_node"] == "hypothesis"
    assert flows[0]["open_step"] == "decision"       # ③ 卡在哪一步


@pytest.mark.asyncio
async def test_flow_open_step_locates_review_and_decision(tmp_path: Path):
    state = _orch_state(tmp_path)
    state.hook_state["pending_post_node_flow"] = [
        _flow_entry("literature", "r-lit", review_state="pending"),
        _flow_entry("data", "r-data", curator_state="done",
                    decision_state="awaiting_human"),
    ]
    closure = compute_orchestration_closure(state, _orch_harness())
    steps = {i["producing_node"]: i["open_step"]
             for i in closure["open_items"] if i["kind"] == "post_node_flow"}
    assert steps == {"literature": "review", "data": "decision"}


@pytest.mark.asyncio
async def test_open_tasks_block_completed_when_producing_was_orchestrated(
        tmp_path: Path):
    """本 run 真派过 producing 节点（且它跑成了），但 TaskList 还有未完成 task。"""
    state = _orch_state(tmp_path)
    _child_run_on_disk(tmp_path, "1785307481-ok0001", "literature", "completed")
    _record_child(state, "literature", "1785307481-ok0001", "completed")
    tl = _tasks(state)
    tl.create("跑 H1 实验", "", "_orchestrator", state.run_id)
    t2 = tl.create("写稿", "", "_orchestrator", state.run_id)
    tl.block(t2.id, "等实验数据齐了才能写")

    summary = await _finalize(state)

    assert summary["status"] == "incomplete"
    tasks = {i["task_id"]: i["status"]
             for i in summary[ORCHESTRATION_CLOSURE_KEY]["open_items"]
             if i["kind"] == "open_task"}
    assert tasks == {"T01": "pending", "T02": "blocked"}
    note = summary[ORCHESTRATION_CLOSURE_KEY]["user_facing_note"]
    assert "T01" in note and "T02" in note


@pytest.mark.asyncio
async def test_background_child_without_terminal_record_blocks_completed(
        tmp_path: Path):
    """background=true 起的子节点还没回报就收尾 —— 最容易被读成"没这回事"。"""
    state = _orch_state(tmp_path)
    state.append_transcript("subagent_call_start", child_node_type="experiment",
                            background=True)
    summary = await _finalize(state)

    assert summary["status"] == "incomplete"
    items = [i for i in summary[ORCHESTRATION_CLOSURE_KEY]["open_items"]
             if i["kind"] == "child_run_no_terminal_record"]
    assert items and items[0]["node_type"] == "experiment"


@pytest.mark.asyncio
async def test_in_flight_child_without_summary_is_not_completed(tmp_path: Path):
    """子 run 目录只有 transcript 没有 summary.json = 还在跑 / 半路没了，都不是 completed。"""
    state = _orch_state(tmp_path)
    d = tmp_path / "1785307482-fly001"
    d.mkdir()
    (d / "transcript.jsonl").write_text(
        json.dumps({"event": "run_start", "node_type": "experiment",
                    "project_id": PROJECT}) + "\n", encoding="utf-8")
    _record_child(state, "experiment", "1785307482-fly001", "incomplete")

    closure = compute_orchestration_closure(state, _orch_harness())
    child = [i for i in closure["open_items"] if i["kind"] == "child_run"]
    assert child and child[0]["status"] == "in_flight"


# ══ ② 防误杀 ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_plain_conversation_turn_with_open_tasks_stays_completed(
        tmp_path: Path):
    """**最容易做错的地方**：纯对话轮（用户只是问了个问题）不许被判 incomplete。

    orchestrator 是长驻节点：chat.py 一个 session 复用同一个 run_id，每解开一次
    pause 链就重写一次 summary.json。此时"项目里还有 pending task"是完全正常的
    中间态。若不加"本 run 编排过 producing 节点"这道前置门，每一轮正常对话都会
    变 incomplete —— 那只是把一个误报换成另一个误报。
    """
    state = _orch_state(tmp_path)
    tl = _tasks(state)
    tl.create("跑 H1 实验", "", "_orchestrator", state.run_id)
    tl.create("写稿", "", "_orchestrator", state.run_id)

    summary = await _finalize(state)

    assert summary["status"] == "completed"
    assert summary["failure_category"] is None
    closure = summary[ORCHESTRATION_CLOSURE_KEY]
    # 不降级 ≠ 不告诉你：未闭环项照样如实列出来
    assert closure["downgrades_status"] is False
    assert closure["closed"] is False
    assert closure["orchestrated_producing_work"] is False
    assert {i["task_id"] for i in closure["open_items"]
            if i["kind"] == "open_task"} == {"T01", "T02"}


@pytest.mark.asyncio
async def test_status_query_turn_reading_artifacts_stays_completed(
        tmp_path: Path):
    """状态查询轮：调了工具、读了 artifact，但没派任何子节点 → 照常 completed。"""
    state = _orch_state(tmp_path)
    state.append_transcript("tool_call", name="query_project_status")
    state.append_transcript("tool_call", name="list_artifacts")
    summary = await _finalize(state)
    assert summary["status"] == "completed"
    assert summary[ORCHESTRATION_CLOSURE_KEY]["closed"] is True


@pytest.mark.asyncio
async def test_retried_child_that_finally_completed_is_not_flagged(
        tmp_path: Path):
    """experiment 第一次 incomplete、REVISE 后第二次 completed = 正常自我纠正。

    "本 run 起过的子节点里有 status != completed"若按字面全量统计，修好了也永远
    incomplete。口径必须是**每个 node_type 只看最近一次**（同 chat.py 终态门禁）。
    """
    state = _orch_state(tmp_path)
    _child_run_on_disk(tmp_path, "1785307479-bad001", "experiment",
                       "incomplete", missing=["experiment_log"])
    _child_run_on_disk(tmp_path, "1785307999-good01", "experiment", "completed")
    _record_child(state, "experiment", "1785307479-bad001", "incomplete")
    _record_child(state, "experiment", "1785307999-good01", "completed")

    summary = await _finalize(state)

    assert summary["status"] == "completed"
    closure = summary[ORCHESTRATION_CLOSURE_KEY]
    assert closure["closed"] is True
    assert closure["orchestrated_producing_nodes"] == ["experiment"]


@pytest.mark.asyncio
async def test_paused_child_resumed_to_completed_is_not_flagged(tmp_path: Path):
    """pause → resume → completed 的子 run 不许被当成"卡在 paused"。

    cascade resume 只替换父的 tool_result，**不会**在父 transcript 补
    subagent_call_end。只信事件就会把一个已经跑完的子 run 永远算成未闭环。
    """
    state = _orch_state(tmp_path)
    _child_run_on_disk(tmp_path, "1785307483-pau001", "data", "completed")
    _record_child(state, "data", "1785307483-pau001", "paused")

    summary = await _finalize(state)

    assert summary["status"] == "completed"
    assert summary[ORCHESTRATION_CLOSURE_KEY]["closed"] is True


@pytest.mark.asyncio
async def test_paused_child_still_paused_is_flagged(tmp_path: Path):
    """反面：磁盘上确实还是 paused → 未闭环。"""
    state = _orch_state(tmp_path)
    _child_run_on_disk(tmp_path, "1785307484-pau002", "data", "paused")
    _record_child(state, "data", "1785307484-pau002", "paused")
    summary = await _finalize(state)
    assert summary["status"] == "incomplete"


@pytest.mark.asyncio
async def test_incomplete_reviewer_child_does_not_downgrade(tmp_path: Path):
    """系统子节点（_reviewer / _curator）的 run status 不参与闭环判定。

    一份精准抓到问题的 review_critique 曾因 enum 漂移把 reviewer run 判成
    incomplete —— 拿它当未闭环信号就是"检查机制反噬检查结果"（框架早前已按
    artifact 角色修过）。它们的权威账本是 pending_post_node_flow。
    """
    state = _orch_state(tmp_path)
    _child_run_on_disk(tmp_path, "1785307485-rev001", "_reviewer", "incomplete")
    _record_child(state, "_reviewer", "1785307485-rev001", "incomplete")

    summary = await _finalize(state)

    assert summary["status"] == "completed"
    closure = summary[ORCHESTRATION_CLOSURE_KEY]
    assert closure["orchestrated_producing_nodes"] == []
    assert closure["open_items"] == []


@pytest.mark.asyncio
async def test_no_project_no_tasks_stays_completed(tmp_path: Path):
    """anon session（无 project_root）：没有 TaskList 可查，不许因此报未闭环。"""
    state = _orch_state(tmp_path, project_id=None)
    summary = await _finalize(state)
    assert summary["status"] == "completed"
    assert summary[ORCHESTRATION_CLOSURE_KEY]["closed"] is True


@pytest.mark.asyncio
async def test_closure_check_has_no_side_effects_on_task_dir(tmp_path: Path):
    """判定层不许有副作用：没建过 task 系统的项目不该被它凭空建出 tasks/ 目录。"""
    state = _orch_state(tmp_path)
    assert state.project_root is not None
    await _finalize(state)
    assert not (state.project_root / "tasks").exists()


# ══ 其它节点不受影响 ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_producing_node_summary_shape_unchanged(tmp_path: Path):
    """producing 节点的完成度仍由它自己的 required_outputs 判，summary 形状不变。"""
    h = NodeHarness(node_type="literature")
    h.required_outputs = []
    # project_id=None → run_end episode hook 自己 skip（不调 LLM）
    state = State.new(node_type="literature", base_dir=tmp_path, project_id=None)
    _record_child(state, "experiment", "1785307486-x", "incomplete")

    summary = await _finalize(state, h)

    assert summary["status"] == "completed"
    assert ORCHESTRATION_CLOSURE_KEY not in summary
    assert compute_orchestration_closure(state, h) is None


# ══ ④ 判据自身失败：不吞不炸 ═════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_closure_failure_is_loud_but_does_not_break_finalize(
        tmp_path: Path, monkeypatch):
    """闭环判据炸了不能连 summary.json 都写不出来（那比误报更糟），但也不许静默
    退回 #221 那个老 bug —— summary 里必须留下 error + 明说 status 未经校验。"""
    import core.executor as ex

    def _boom(state, harness):
        raise RuntimeError("账本读挂了")

    monkeypatch.setattr(ex, "compute_orchestration_closure", _boom)
    state = _orch_state(tmp_path)
    summary = await _finalize(state)

    assert summary["status"] == "completed"          # 不 fail-closed 误杀
    closure = summary[ORCHESTRATION_CLOSURE_KEY]
    assert closure["closed"] is None                 # "查过且闭环" ≠ "没查成"
    assert "账本读挂了" in closure["error"]
    assert "未经" in closure["user_facing_note"]
    events = [json.loads(x) for x in
              state.transcript_path.read_text(encoding="utf-8").splitlines()]
    assert any(e.get("event") == "orchestration_closure_failed" for e in events)


# ══ ④ harness prompt 侧：未闭环必须如实说 ════════════════════════════════════

def test_orchestrator_harness_forbids_claiming_completion():
    """面向用户的汇报口径也要同步（否则机械账降级了、模型嘴上还在报完成）。"""
    from core.loader import load_harness

    h = load_harness("_orchestrator")
    blob = (h.system_prompt or "") + "\n".join(h.rules or [])
    assert "#221" in blob
    assert "pending_post_node_flow" in blob
    assert "orchestration_closure" in blob
