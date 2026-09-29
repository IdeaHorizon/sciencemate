"""「编排的工作闭环了没」—— 唯一权威推导（core/closure.py）的跨消费者回归。

## 背景

这个判据曾经有**两份实现**：

  - `chat.py` 的 continuous 终态门禁 —— 拦模型自报的 `CONTINUOUS_STATUS: complete`；
  - `core/executor.py` 的 #221 run status 判据 —— 拦 summary.json 自报的 `completed`。

两处读同一批机械账本、口径今天也一致（"条目还在 pending_post_node_flow 里就没走
完"、"每个 node_type 只看最近一次"、"磁盘对账走 core.run_history"）—— 但**没有任何
东西保证它们继续一致**。而"没有单一权威推导"的后果 `core/run_history.py` 的模块文档
已经写过一遍：9 处各自推导"某个 run 现在什么状态"，口径漂移，同一天四个 PR 在收拾
后果，其中一个 PR 的第一版还等于又加了第 9 个不一致的实现。

## 这个文件测两件事

  ① **判据只有一份实现**：消费方不许自己再扫一遍 transcript / 再对一遍磁盘
     （结构性断言 —— 它挡的是"又抄了一份"，那是所有口径漂移的起点）；
  ② **刻意保留的差异不许被"顺手统一"掉**：每条差异都有实测理由，写在
     `core/closure.py` 的 ClosureScope 上。把它们抹平会各自复发一个老 bug，
     所以这里逐条钉住 —— 包括反方向（该覆盖的没覆盖）。

全部离线：手搓 transcript 事件 / 假 summary.json / hook_state / 临时 TaskList，
不调 LLM、不联网。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import chat as chat_mod
from core import closure
from core.agent_loop import LoopResult
from core.bootstrap import bootstrap
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

bootstrap()

PROJECT = "p_closure"

APPEAL = {
    "requested_by_node": "data",
    "upstream_node": "experiment",
    "missing": "approved plan（没有它 data 无法确定抽样口径）",
    "acceptance": "plan artifact 冻结且 scope 覆盖本次抽样",
    "blocking": True,
}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    clear_all()


# ── 构件 ────────────────────────────────────────────────────────────────────

def _orch_harness() -> NodeHarness:
    h = NodeHarness(node_type="_orchestrator")
    h.required_outputs = []
    return h


def _orch_state(tmp_path: Path, project_id: str | None = PROJECT) -> State:
    return State.new(node_type="_orchestrator", base_dir=tmp_path,
                     project_id=project_id)


def _run_on_disk(base_dir: Path, run_id: str, node_type: str, status: str, *,
                 project_id: str | None = PROJECT,
                 missing: list[str] | None = None,
                 appeals: list[dict] | None = None) -> None:
    d = base_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "run_id": run_id, "node_type": node_type, "project_id": project_id,
        "status": status,
        "missing_required_outputs": missing or [],
        "quality_check_results": [],
        "upstream_rework_requests": appeals or [],
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
    elif status is not None:
        state.append_transcript("subagent_call_end", child_node_type=node_type,
                                child_run_id=run_id, child_status=status,
                                child_turns=3)


def _flow_entry(node: str = "hypothesis", run_id: str = "1785307000-aaa111",
                **overrides) -> dict:
    entry = {
        "producing_node": node, "producing_run_id": run_id, "task_id": None,
        "artifact_ids": ["a1"], "review_state": "done",
        "curator_state": "pending", "decision_state": "pending",
    }
    entry.update(overrides)
    return entry


def _continuous_accepts_complete(state: State) -> bool:
    """continuous 终态门禁放不放过模型自报的 complete。"""
    state.hook_state.update(continuous_loop=True, continuous_phase="running")
    prompt, _ = chat_mod._continuous_followup(
        state, "已经全部交付\nCONTINUOUS_STATUS: complete", reason="turn_finished")
    return prompt is None and state.hook_state["continuous_phase"] == "complete"


async def _finalize(state: State) -> dict:
    lr = LoopResult(final_text="已按计划推进。", turns=4,
                    tool_calls=[{"id": "c1", "function": {"name": "run_node"}}])
    return await finalize_run(state, _orch_harness(), lr, llm=None)


# ══ ① 判据只有一份实现 ══════════════════════════════════════════════════════

def test_only_the_closure_module_scans_child_run_events():
    """扫子 run 事件的代码只许有一份。

    两个消费方各扫一遍 transcript 就是口径漂移的起点：一边认 `subagent_call_paused`
    一边不认、一边补孤儿 run 一边不补 —— 这些差异都真实存在过。谁再抄一份，这条
    断言先响，而不是等某次 E2E 里"项目关不掉 / 报了完成其实没完"。
    """
    root = Path(__file__).resolve().parent.parent
    assert "subagent_call_end" in (root / "core" / "closure.py").read_text(
        encoding="utf-8")
    for consumer in ("chat.py", "core/executor.py"):
        body = (root / consumer).read_text(encoding="utf-8")
        assert "subagent_call_end" not in body, (
            f"{consumer} 又在自己扫子 run 事件了 —— 判据归 core/closure.py，"
            f"消费方只做投影（见 core/run_history.py 模块文档：9 处各自推导 → "
            f"同一天四个 PR 收拾后果）")
        assert "import closure" in body, (
            f"{consumer} 没走 core/closure.py 这个权威推导")


@pytest.mark.asyncio
async def test_both_consumers_agree_that_unresolved_writing_is_not_closed(
        tmp_path: Path):
    """同一份磁盘状态：终态门禁驳回 complete，run status 也必须不算 completed。

    两条路径**同时**对同一个事实表态，是这次并轨的核心不变量：一边拒绝收尾、另一边
    在 summary.json 里写 `completed`，就是 #221 换了个字段原样复发。
    """
    state = _orch_state(tmp_path)
    _run_on_disk(tmp_path, "1785307479-w0001", "writing", "incomplete",
                 missing=["manuscript"])
    _record_child(state, "writing", "1785307479-w0001", "incomplete")

    orch = compute_orchestration_closure(state, _orch_harness())
    assert orch["closed"] is False and orch["downgrades_status"] is True
    summary = await _finalize(state)
    assert summary["status"] == "incomplete"
    assert summary["failure_category"] == FAILURE_CATEGORY_ORCHESTRATION_NOT_CLOSED

    assert _continuous_accepts_complete(state) is False


@pytest.mark.asyncio
async def test_both_consumers_agree_when_the_work_really_closed(tmp_path: Path):
    """反面：真闭环了两边都必须放行（门禁的价值等于它放行的准确度）。"""
    state = _orch_state(tmp_path)
    _run_on_disk(tmp_path, "1785307999-w0002", "writing", "completed")
    _record_child(state, "writing", "1785307999-w0002", "completed")

    orch = compute_orchestration_closure(state, _orch_harness())
    assert orch["closed"] is True and orch["downgrades_status"] is False
    summary = await _finalize(state)
    assert summary["status"] == "completed"

    assert _continuous_accepts_complete(state) is True


@pytest.mark.asyncio
async def test_flow_entry_presence_is_the_only_flow_judgement(tmp_path: Path):
    """三个 state 字段都不在"开放态"、条目却还挂在队列上 —— 两边都算未闭环。

    出列只发生在 decision 被机械记账那一刻。若哪边改成"按 state 枚举判 done"，
    一次 enum 漂移就能让 flow 静默消失（review 门完整性那一串 PR 反复在修这个）。
    """
    state = _orch_state(tmp_path)
    state.hook_state["pending_post_node_flow"] = [
        _flow_entry(review_state="done", curator_state="done",
                    decision_state="done")]

    orch = compute_orchestration_closure(state, _orch_harness())
    flows = [i for i in orch["open_items"] if i["kind"] == "post_node_flow"]
    assert flows and flows[0]["open_step"] == "queued_unknown_step"
    # flow 条目本身就是"本 run 编排过 producing 工作"的证据 → 该降级
    assert orch["downgrades_status"] is True

    assert _continuous_accepts_complete(state) is False


# ══ ② 刻意保留的差异 ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_continuous_gate_stays_narrowed_to_writing(tmp_path: Path):
    """差异 1：终态门禁只看 writing，run status 看全部 producing 节点。

    终态门禁放过上游的 incomplete/cancelled 尝试是**故意的**：后来的
    project_synthesis 决策可以合法取代一个被放弃的 literature / data / hypothesis
    尝试。而 run status 侧不能放过任何一个 —— 它汇报的就是"这一 run 派出去的活儿
    完没完"，放过一个就是 #221 的原始现场（experiment incomplete，汇总报完成）。
    """
    state = _orch_state(tmp_path)
    _run_on_disk(tmp_path, "1785307479-d15500", "experiment", "incomplete",
                 missing=["experiment_log"])
    _record_child(state, "experiment", "1785307479-d15500", "incomplete")

    # run status 侧：必须抓住
    orch = compute_orchestration_closure(state, _orch_harness())
    child = [i for i in orch["open_items"] if i["kind"] == "child_run"]
    assert [i["node_type"] for i in child] == ["experiment"]
    assert orch["downgrades_status"] is True

    # continuous 侧：故意放行（收窄口径必须活下来）
    assert _continuous_accepts_complete(state) is True


@pytest.mark.asyncio
async def test_project_tasks_gate_only_applies_to_run_status(tmp_path: Path):
    """差异 2：TaskList / curator 镜像只有 run status 侧看。

    TaskList 是**项目级长账**。continuous 若拿它当终态门禁，一条没人关掉的陈旧
    task 就能让自动续轮永远停不下来（持续烧算力）；run status 侧只是如实汇报一次，
    没有这个风险。
    """
    state = _orch_state(tmp_path)
    _run_on_disk(tmp_path, "1785307481-ok0001", "literature", "completed")
    _record_child(state, "literature", "1785307481-ok0001", "completed")
    assert state.project_root is not None
    TaskList(state.project_root / "tasks").create(
        "跑 H1 实验", "", "_orchestrator", state.run_id)

    orch = compute_orchestration_closure(state, _orch_harness())
    assert [i["kind"] for i in orch["open_items"]] == ["open_task"]
    assert orch["downgrades_status"] is True

    assert _continuous_accepts_complete(state) is True


@pytest.mark.asyncio
async def test_run_status_downgrades_on_open_blocking_obligation(tmp_path: Path):
    """差异 3（本次并轨的决定）：blocking 义务现在两条路径都看。

    原来只有 chat.py 看 core.obligations。于是"终态门禁因为一条未了结的申诉拒绝
    complete，同一刻 summary.json 写着 completed"是可能的 —— 平台汇总读的是后者，
    #221 换了个字段复发。E2E-4 现场（data 申诉"我需要 approved plan"，没人跟进，
    orchestrator 自己改道绕开）本来就该在 run status 上看得见。
    """
    state = _orch_state(tmp_path)
    # 申诉挂在 data 上（experiment 始终没跑 → 账没了结）
    _run_on_disk(tmp_path, "1785307500-d0001", "data", "incomplete",
                 appeals=[APPEAL])
    # 本 run 确实编排过 producing 工作，且那次跑成了 → 唯一的未闭环项是这笔账
    _run_on_disk(tmp_path, "1785307600-l0001", "literature", "completed")
    _record_child(state, "literature", "1785307600-l0001", "completed")

    summary = await _finalize(state)
    orch = summary[ORCHESTRATION_CLOSURE_KEY]
    items = [i for i in orch["open_items"] if i["kind"] == "blocking_obligation"]
    assert items, orch["open_items"]
    assert items[0]["owed_by"] == "experiment"
    assert "approved plan" in items[0]["what"]
    assert summary["status"] == "incomplete"
    assert summary["failure_category"] == FAILURE_CATEGORY_ORCHESTRATION_NOT_CLOSED
    # 汇报口径也要带上，否则模型嘴上照样报完成
    assert "未了结义务" in orch["user_facing_note"]

    # 同一份账在 continuous 侧也拒绝收尾（两边一致）
    assert _continuous_accepts_complete(state) is False


@pytest.mark.asyncio
async def test_stale_obligation_alone_does_not_downgrade_a_conversation_turn(
        tmp_path: Path):
    """防误杀不许被义务这条项目级信号绕过。

    orchestrator 是长驻节点：chat.py 一个 session 复用同一个 run_id，每解开一次
    pause 链就重写一次 summary.json。用户只是问了句状态时，"项目里还欠着一笔上一轮
    留下的账"是完全正常的中间态 —— 义务**不算**"本 run 编排过 producing 工作"的
    证据（它可能来自上一个 run）。否则每一轮正常对话都会变 incomplete：那只是把一个
    误报换成另一个。
    """
    state = _orch_state(tmp_path)
    _run_on_disk(tmp_path, "1785307500-d0002", "data", "incomplete",
                 appeals=[APPEAL])

    summary = await _finalize(state)
    orch = summary[ORCHESTRATION_CLOSURE_KEY]
    assert summary["status"] == "completed"
    assert summary["failure_category"] is None
    assert orch["downgrades_status"] is False
    assert orch["orchestrated_producing_work"] is False
    # 不降级 ≠ 不告诉你：这笔账照样如实列出来
    assert orch["closed"] is False
    assert [i["kind"] for i in orch["open_items"]] == ["blocking_obligation"]


# ══ 并轨时统一掉的三处漂移（各自钉住，防止悄悄退回）════════════════════════

@pytest.mark.asyncio
async def test_missing_transcript_does_not_blind_disk_reconciliation(
        tmp_path: Path):
    """transcript 不存在**不等于**没有子 run —— 磁盘才是事实来源。

    并轨前 chat.py 那份实现开头是 `if not transcript.exists(): return []`，于是
    "父进程还没写过任何事件"这一种情况下磁盘上的 incomplete writing run 完全隐形，
    complete 直接放行。这正是 core/run_history.py 反复警告的"信 transcript 的沉默"。
    """
    state = _orch_state(tmp_path)
    assert not state.transcript_path.exists()
    _run_on_disk(tmp_path, "1785307700-w0003", "writing", "incomplete",
                 missing=["manuscript"])

    work = closure.open_work(state, closure.CONTINUOUS_TERMINAL)
    assert [a.run_id for a in work.unresolved_producers] == ["1785307700-w0003"]
    assert _continuous_accepts_complete(state) is False


@pytest.mark.asyncio
async def test_paused_writing_child_blocks_continuous_complete(tmp_path: Path):
    """暂停着的 writing 子 run 不是闭环。

    并轨前 chat.py 只认 `subagent_call_end`（要求带 status），`subagent_call_paused`
    整类事件看不见 —— 一个正卡在人工决策上的 writing 子 run 于是拦不住 complete。
    executor 那份一直是认的；统一到认。
    """
    state = _orch_state(tmp_path)
    _run_on_disk(tmp_path, "1785307800-w0004", "writing", "paused")
    _record_child(state, "writing", "1785307800-w0004", "paused")

    assert _continuous_accepts_complete(state) is False
    orch = compute_orchestration_closure(state, _orch_harness())
    assert orch["downgrades_status"] is True


@pytest.mark.asyncio
async def test_orphan_completed_run_closes_a_started_node_for_run_status(
        tmp_path: Path):
    """run status 侧也要认孤儿 run（E2E-3 的修法，原来只有 chat.py 有）。

    子 run 跑完但**父进程在它写下 subagent_call_end 之前就没了**，成功的那次对父
    完全隐形。并轨前 run status 侧只会看到"起过它但没有终态记录"，于是永远
    incomplete —— 和 chat.py 那边"项目永远关不掉"是同一个 bug 的另一半。
    """
    state = _orch_state(tmp_path)
    # 父只记下了"起过"，终态事件没来得及写
    state.append_transcript("subagent_call_start", child_node_type="experiment",
                            child_depth=1)
    _run_on_disk(tmp_path, "1785307900-e0005", "experiment", "completed")

    orch = compute_orchestration_closure(state, _orch_harness())
    assert orch["open_items"] == []
    assert orch["closed"] is True
    assert orch["orchestrated_producing_nodes"] == ["experiment"]
    summary = await _finalize(state)
    assert summary["status"] == "completed"


@pytest.mark.asyncio
async def test_orphan_run_is_not_counted_as_this_run_orchestrating_it(
        tmp_path: Path):
    """磁盘对账捞回来的孤儿 run **不算**"本 run 派过它"。

    否则项目里任何一个别的 run 派的活儿都会变成本 run 的降级理由 —— 防误杀门被
    磁盘对账从背后拆掉。证据必须在对账**之前**定格。
    """
    state = _orch_state(tmp_path)
    # 本 run transcript 一个子 run 事件都没有；磁盘上有一个别处派的失败 writing run
    _run_on_disk(tmp_path, "1785308000-w0006", "writing", "incomplete",
                 missing=["manuscript"])

    work = closure.open_work(state, closure.CONTINUOUS_TERMINAL)
    assert work.unresolved_producers                      # 事实照报
    assert work.orchestrated_producing_nodes == ()        # 但不算本 run 编排过
    assert work.orchestrated_producing_work is False


def test_harness_advertises_every_signal_that_downgrades_run_status():
    """广告口径必须与机械门禁一致。

    harness.yaml 里那条 MUST NOT 逐项列出了"有这些就不许说完成"。机械层新增一类
    降级信号（本次：blocking 义务）而提示词没跟上，就变成"框架降级了、模型嘴上还在
    报完成" —— 用户看到的仍然是"任务已完成"，#221 的用户可见症状原样保留。
    """
    from core.loader import load_harness

    blob = (load_harness("_orchestrator").system_prompt or "") + "\n".join(
        load_harness("_orchestrator").rules or [])
    assert "#221" in blob
    for advertised in ("pending_post_node_flow", "orchestration_closure",
                       "未了结的义务", "core/closure.py"):
        assert advertised in blob, f"harness 没告诉模型 {advertised} 这条账"


def test_closure_judgement_has_no_side_effects(tmp_path: Path):
    """判定层只读：不建目录、不写状态。"""
    state = _orch_state(tmp_path)
    assert state.project_root is not None
    before = sorted(p.name for p in tmp_path.iterdir())

    closure.open_work(state, closure.RUN_STATUS)
    closure.open_work(state, closure.CONTINUOUS_TERMINAL)

    assert not (state.project_root / "tasks").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert state.hook_state == {}
