"""#143 gap 2+3：producer→reviewer 机械 gate + run/task 生命周期绑定。

gap 2：orchestrator 曾能对一个 incomplete producer 直接起 _reviewer，再用
read_external_artifact 把隔离产物读回来绕过隔离。现在框架在 reviewer 启动前
机械验证存在 matching pending_post_node_flow（只有 completed+qc全过 的 producer
才有），否则返回 error、不启动 reviewer。

gap 3：run_node 收 task_id，child incomplete/error/cancelled 时框架确定性 block
绑定 task（写原因 + run id），task ledger 不再永久卡 in_progress。

不联网、无真实 LLM。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.bootstrap import bootstrap

bootstrap()

from core.artifact_provenance import produced  # noqa: E402
from core.harness import NodeHarness  # noqa: E402
from core.ledger import RecordStore  # noqa: E402
from core.state import State  # noqa: E402
from core.tool_registry import execute as execute_tool  # noqa: E402
from shared.tools.library.tasks import _get_task_list  # noqa: E402
from shared.tools.run_node import (  # noqa: E402
    _finish_child,
    _task_block_best_effort,
    _task_start_best_effort,
    recover_interrupted_decision_actions,
)


def _child_record(cdir: Path, *, node_type: str, run_id: str, artifact_type: str,
                  name: str, content: str, metadata: dict | None = None,
                  created_at: str = "2026-07-14T00:00:00Z") -> str:
    """往子 run 目录的 run 本地账本落一份记录（原生文件 + records.jsonl 一行）。

    `_finish_child` 的回填只认子 run 的账本，不再读 `<run>/artifacts/<id>.json` 信封。
    """
    artifact_id = f"{artifact_type}__{name}"
    RecordStore(cdir / "artifacts", cdir / "records.jsonl").save(
        artifact_id=artifact_id, artifact_type=artifact_type, name=name,
        content=content, metadata=dict(metadata or {}), directory=cdir / "artifacts",
        created_at=created_at, provenance=produced(node_type, run_id),
        produced_by_node_type=node_type, produced_by_run_id=run_id,
        by_node=node_type, by_run=run_id,
    )
    return artifact_id


# ── 2026-08-19：下面这些用例连同它们钉的两道墙一起删除 ──────────────────
#
#   test_reviewer_blocked_when_no_pending_flow
#   test_reviewer_wrong_producer_id_blocked
#   test_143_guard_not_regressed_by_151
#   test_finalized_review_cannot_be_retried_with_clear_message
#   test_reviewer_retry_blocked_before_human_authorizes
#   test_downstream_blocked_while_review_not_done
#   test_downstream_blocked_when_review_done_but_no_critique
#   test_revise_authorization_blocks_every_other_producer
#   test_incomplete_producer_cannot_pull_a_reviewer_in_project_v2
#
# 它们钉的是「起 _reviewer 的资格门」与「flow 没闭合不许起新 producing 节点」的
# **报错文案与拦截行为**。两道墙已删 —— 不是不再需要防，是它们防的那个窗口不
# 存在了：producing 交付之后，直到 flow 闭合，控制权一次都不回调度器手里
# （review/呈递归运行时、授权动作归运行时执行、EDIT 与派发失败都保持 pause）。
#
# 替代它们的是**不变量本身**，不是墙的行为：
#   tests/test_orchestrator_never_holds_an_open_flow.py
# 窗口一旦回来，那条先红 —— 那时该关窗口，不是把墙加回来。
#
# 空转熔断（_MAX_ACTION_ATTEMPTS）没被删，另有用例守着。

def _orch_state(td: Path, project_id="p143") -> State:
    s = State.new(node_type="_orchestrator", base_dir=td, project_id=project_id)
    s.hook_state["_callable_nodes"] = ["*"]
    return s


# ── gap 2：reviewer gate ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reviewer_not_gate_blocked_when_matching_pending_flow():
    """completed producer（有 matching pending entry）→ 不被本 gate 拦（可能因别的
    原因失败，但不能是 'no matching pending' 这条）。"""
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        state.hook_state["pending_post_node_flow"] = [{
            "producing_node": "hypothesis", "producing_run_id": "r_ok",
            "artifact_ids": ["x"], "review_state": "pending",
            "curator_state": "pending", "decision_state": "pending",
        }]
        res = await execute_tool(
            "run_node", state,
            node_type="_reviewer", user_note="测试派发",
            node_inputs={"artifact_id": "x", "source_node_type": "hypothesis",
                         "producer_run_id": "r_ok"},
        )
        # 不该是"没有 matching pending flow"这条 gate error
        if res.get("status") == "error":
            assert "没有 matching pending_post_node_flow" not in res.get("error", "")


@pytest.mark.asyncio
async def test_reviewer_project_scope_not_gated():
    """source_node_type='_project'（项目级 synthesis reviewer）不被 producer gate 拦。"""
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        res = await execute_tool(
            "run_node", state,
            node_type="_reviewer", user_note="测试派发",
            node_inputs={"source_node_type": "_project", "project_id": "p143"},
        )
        # 无 pending flow 也不该被 gap-2 gate 拦（会因别的原因走，但不是这条）
        if res.get("status") == "error":
            assert "没有 matching pending_post_node_flow" not in res.get("error", "")


# ── gap 3：task 生命周期绑定 ─────────────────────────────────────────────────

def _make_started_task(state: State):
    tl = _get_task_list(state)
    t = tl.create("Literature stage", "调研", owner_node="_orchestrator",
                  run_id=state.run_id)
    tl.start(t.id, "_orchestrator")
    return tl, t.id


def _lit_harness():
    # v2.0：literature 已服务化（post_run_flow: none），producing 语义的用例
    # 改用 hypothesis —— 它是真 producing 节点，会登记 post-node flow。
    return NodeHarness(node_type="hypothesis", system_prompt="",
                       required_output_artifact_types=["pre_registration"])


def test_finish_child_blocks_task_on_incomplete():
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        tl, tid = _make_started_task(state)
        cdir = state.root / "child_lit_x"
        cdir.mkdir(parents=True, exist_ok=True)
        summary = {"run_id": "r_lit_x", "node_type": "hypothesis",
                   "status": "incomplete", "turns": 5,
                   "missing_required_outputs": ["hypothesis_set"],
                   "state_dir": str(cdir), "artifacts": []}
        out = _finish_child(state, "hypothesis", {}, summary, _lit_harness(),
                            None, task_id=tid)
        t = tl.get(tid)
        assert t.status == "blocked"
        assert "r_lit_x" in t.blocked_reason
        assert "incomplete" in t.blocked_reason
        assert out["can_start_standard_review"] is False
        assert out["task_id"] == tid


def test_finish_child_blocks_task_on_cancelled():
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        tl, tid = _make_started_task(state)
        cdir = state.root / "child_c"
        cdir.mkdir(parents=True, exist_ok=True)
        summary = {"run_id": "r_c", "node_type": "hypothesis",
                   "status": "cancelled", "turns": 2,
                   "state_dir": str(cdir), "artifacts": []}
        _finish_child(state, "hypothesis", {}, summary, _lit_harness(),
                      None, task_id=tid)
        assert tl.get(tid).status == "blocked"


def test_finish_child_leaves_task_on_completed():
    """completed → 不 block（常规 review→decision 流程接管）。"""
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        tl, tid = _make_started_task(state)
        cdir = state.root / "child_ok"
        cdir.mkdir(parents=True, exist_ok=True)
        summary = {"run_id": "r_ok", "node_type": "hypothesis",
                   "status": "completed", "turns": 8,
                   "state_dir": str(cdir), "artifacts": []}
        _finish_child(state, "hypothesis", {}, summary, _lit_harness(),
                      None, task_id=tid)
        assert tl.get(tid).status == "in_progress"   # 保持，不被 block


def test_task_helpers_safe_without_project_root():
    """无 project_root（run-local state）→ task 系统不可用，helper 不炸、no-op。"""
    with tempfile.TemporaryDirectory() as td:
        state = State.new(node_type="_orchestrator", base_dir=Path(td))  # 无 project_id
        _task_start_best_effort(state, "T99", "_orchestrator")   # 不抛
        _task_block_best_effort(state, "T99", "reason")          # 不抛


def test_task_id_none_is_noop():
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        _task_start_best_effort(state, None, "_orchestrator")
        _task_block_best_effort(state, None, "reason")


# ── #151：reviewer 挂了之后的 reviewer-only retry 恢复路径 ────────────────────
# 死路复现：producer 合格(entry 存在) → reviewer 截断没产 critique(review_state=
# failed) → decision package 呈递后把 entry 连锅端掉 → #143 guard 再也找不到
# matching entry → 只剩"重跑好 producer"或"没 critique 就 PROCEED"。

def _reviewer_harness():
    return NodeHarness(node_type="_reviewer", system_prompt="",
                       required_output_artifact_types=["review_critique"])


def _eligible_flow_entry(producing_run_id="r_prod_ok"):
    return {
        "producing_node": "hypothesis", "producing_run_id": producing_run_id,
        "artifact_ids": ["pre_registration__revised"],
        "review_state": "pending", "review_critique_artifact_id": None,
        "review_failed_reason": None,
        "curator_state": "pending", "decision_state": "pending",
    }


def _truncated_reviewer_summary(run_id="r_rev_trunc"):
    """reviewer 撞 max_output_tokens：incomplete + 没有 review_critique artifact。"""
    return {"run_id": run_id, "node_type": "_reviewer", "status": "incomplete",
            "turns": 10, "missing_required_outputs": ["review_critique"],
            "artifacts": []}


def test_failed_reviewer_marks_entry_retryable():
    """A：reviewer 没产 critique → review_state=failed + retryable + attempt 计数。"""
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        state.hook_state["pending_post_node_flow"] = [_eligible_flow_entry()]
        cdir = state.root / "rev1"
        cdir.mkdir(parents=True, exist_ok=True)
        s = _truncated_reviewer_summary()
        s["state_dir"] = str(cdir)
        _finish_child(state, "_reviewer",
                      {"source_node_type": "hypothesis", "producer_run_id": "r_prod_ok"},
                      s, _reviewer_harness(), None)
        e = state.hook_state["pending_post_node_flow"][0]
        assert e["review_state"] == "failed_awaiting_human"   # #155：先等人工授权
        assert e["review_retryable"] is True
        assert e["review_attempt_count"] == 1
        assert "未产出 review_critique" in e["review_failed_reason"]


def test_decision_package_keeps_failed_entry_as_retry_credential():
    """E/核心：decision package 呈递后，failed 的 entry **不能**被删 —— 它是唯一恢复凭据。"""
    import asyncio as _a

    from shared.tools.library.decision_package import _present_decision_package
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        entry = _eligible_flow_entry()
        entry.update({"review_state": "failed", "curator_state": "done",
                      "review_failed_reason": "reviewer 截断，无 critique"})
        state.hook_state["pending_post_node_flow"] = [entry]
        _a.run(_present_decision_package(
            state, source_node_type="hypothesis", producing_run_id="r_prod_ok",
            producing_summary="18 turns", artifact_ids_produced=["pre_registration__revised"],
            curator_summary="+2 claims", review_failed_reason="reviewer 截断，无 critique",
        ))
        flow = state.hook_state["pending_post_node_flow"]
        assert len(flow) == 1, "failed 的 flow entry 被删了 → reviewer retry 死路（#151）"
        assert flow[0]["review_state"] == "failed_awaiting_human"
        assert flow[0]["review_retryable"] is True
        # #155 纠正：我在 #151 里断言过 decision_state=="done" 且注释"不卡新
        # producing" —— 那是错的，等于呈递本身就放行下游（没有有效 critique 也能
        # 进下一阶段）。呈递只是**等人工**，真正的 done 由 record_decision_answer
        # 在拿到答复后写。
        assert flow[0]["decision_state"] == "awaiting_human"


@pytest.mark.asyncio
async def test_reviewer_retry_allowed_after_failed_review():
    """B：failed 的 eligible entry 存在 → reviewer-only retry 被放行（不再被 guard 拦）。"""
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        entry = _eligible_flow_entry()
        entry.update({"review_state": "retry_authorized", "review_retryable": True,
                      "curator_state": "done", "decision_state": "done",
                      "retry_authorized_by": "human_decision_package",
                      "review_failed_reason": "截断"})
        state.hook_state["pending_post_node_flow"] = [entry]
        res = await execute_tool(
            "run_node", state, node_type="_reviewer", user_note="测试派发",
            node_inputs={"source_node_type": "hypothesis",
                         "producer_run_id": "r_prod_ok",
                         "artifact_id": "pre_registration__revised"},
        )
        if res.get("status") == "error":
            assert "不能对" not in res.get("error", ""), (
                f"reviewer retry 被 guard 误拦（#151 死路）：{res.get('error')}"
            )


def test_reviewer_retry_success_flips_entry_to_done():
    """B：retry 产出 critique → entry 翻 done（不能永远卡 failed）。"""
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        entry = _eligible_flow_entry()
        entry.update({"review_state": "retry_authorized", "review_retryable": True,
                      "curator_state": "done", "decision_state": "done",
                      "review_attempt_count": 1})
        state.hook_state["pending_post_node_flow"] = [entry]
        # 造一个真的产出了 review_critique 的 reviewer run
        cdir = state.root / "rev2"
        _child_record(cdir, node_type="_reviewer", run_id="r_rev_ok",
                      artifact_type="review_critique", name="c", content="{}",
                      metadata={"verdict": "approve"})
        s = {"run_id": "r_rev_ok", "node_type": "_reviewer", "status": "completed",
             "turns": 6, "state_dir": str(cdir),
             "artifacts": [{"id": "review_critique__c", "type": "review_critique",
                            "name": "c"}]}
        _finish_child(state, "_reviewer",
                      {"source_node_type": "hypothesis", "producer_run_id": "r_prod_ok"},
                      s, _reviewer_harness(), None)
        e = state.hook_state["pending_post_node_flow"][0]
        assert e["review_state"] == "done"
        assert e["review_critique_artifact_id"] == "review_critique__c"
        assert e["review_attempt_count"] == 2          # 累计两次尝试，可审计
        assert "review_retryable" not in e


def test_second_reviewer_failure_stays_retryable_with_history():
    """C：retry 又挂 → 仍 failed/retryable，attempt 累加（不自动无限循环、不放行）。"""
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        entry = _eligible_flow_entry()
        entry.update({"review_state": "retry_authorized", "review_retryable": True,
                      "curator_state": "done", "decision_state": "done",
                      "review_attempt_count": 1})
        state.hook_state["pending_post_node_flow"] = [entry]
        cdir = state.root / "rev3"
        cdir.mkdir(parents=True, exist_ok=True)
        s = _truncated_reviewer_summary("r_rev_trunc2")
        s["state_dir"] = str(cdir)
        _finish_child(state, "_reviewer",
                      {"source_node_type": "hypothesis", "producer_run_id": "r_prod_ok"},
                      s, _reviewer_harness(), None)
        e = state.hook_state["pending_post_node_flow"][0]
        assert e["review_state"] == "failed_awaiting_human"   # 不自动第三次，回等人工
        assert e["review_retryable"] is True
        assert e["review_attempt_count"] == 2
        assert e["review_last_run_id"] == "r_rev_trunc2"


def _failed_review_entry(**over):
    e = _eligible_flow_entry()
    e.update({"review_state": "failed_awaiting_human", "review_retryable": True,
              "review_failed_reason": "reviewer 截断，无 critique",
              "review_attempt_count": 1})
    e.update(over)
    return e


# ── A：人工授权才能 reviewer-only retry ────────────────────────────────────

def test_decision_package_offers_retry_reviewer_and_no_proceed():
    """A：review 无可用 critique → 选项集以 RETRY REVIEWER 打头且**不含 PROCEED**。"""
    from shared.tools.library.decision_package import (
        _REVIEW_FAILED_ACTIONS,
        _render_decision_package,
        build_decision_offer,
    )
    # 选项集只有一处声明；正文是它的投影，不再各自构造（2026-08-19 事故）。
    offer = build_decision_offer(
        decision_id="r_prod_ok:ptest",
        source_node_type="hypothesis",
        action_ids=list(_REVIEW_FAILED_ACTIONS),
        recommended_action="retry_reviewer",
    )
    assert offer.choice_ids()[0] == "retry_reviewer"
    assert "proceed" not in offer.choice_ids()

    text = _render_decision_package(
        source_node_type="hypothesis", producing_run_id="r_prod_ok",
        producing_summary="18 turns", artifact_ids_produced=["pre_registration__x"],
        curator_summary="", review_critique_json=None,
        review_failed_reason="reviewer 截断", review_unusable=True,
        offer=offer, recommended_feedback="",
    )
    assert "RETRY REVIEWER" in text and "← recommended" in text
    assert "不提供 PROCEED" in text
    # 正文里不许出现选项集之外的动作 —— 事故正是这条被破坏的。
    assert "PROCEED to next stage" not in text


def test_record_decision_answer_authorizes_retry():
    """A：人工选 [1] → review_state=retry_authorized + 留人工授权痕迹。"""
    from shared.tools.library.decision_package import record_decision_answer
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        entry = _failed_review_entry(
            decision_state="awaiting_human",
            decision_options=["retry_reviewer", "revise", "redirect_upstream",
                              "abort", "edit"])
        state.hook_state["pending_post_node_flow"] = [entry]
        out = record_decision_answer(
            state, {"type": "decision_package", "producing_run_id": "r_prod_ok"}, "1")
        assert out["review_state"] == "retry_authorized"
        assert out["accepted_action"] == "retry_reviewer"
        assert out["retry_authorized_by"] == "human_decision_package"
        # entry 仍在（凭据保留），且 decision 已记
        assert len(state.hook_state["pending_post_node_flow"]) == 1
        assert out["decision_state"] == "done"


@pytest.mark.asyncio
async def test_reviewer_retry_allowed_after_human_authorizes():
    """A：人工授权后（retry_authorized）同一 producer 的 reviewer 被放行。"""
    from shared.tools.library.decision_package import record_decision_answer
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        state.hook_state["pending_post_node_flow"] = [_failed_review_entry(
            decision_state="awaiting_human",
            decision_options=["retry_reviewer", "revise", "redirect_upstream",
                              "abort", "edit"])]
        record_decision_answer(
            state, {"type": "decision_package", "producing_run_id": "r_prod_ok"}, "1")
        res = await execute_tool(
            "run_node", state, node_type="_reviewer", user_note="测试派发",
            node_inputs={"source_node_type": "hypothesis",
                         "producer_run_id": "r_prod_ok"})
        if res.get("status") == "error":
            assert "人工还没授权" not in res.get("error", "")
            assert "没有 matching" not in res.get("error", "")


# ── B：review 没成功 → 下游 producing 一律 fail-closed ─────────────────────

@pytest.mark.asyncio
async def test_downstream_allowed_after_full_chain_and_human_proceed():
    """正常路径不回归：critique 有 + curator done + 人工 PROCEED → flow 关闭 → 放行。"""
    from shared.tools.library.decision_package import record_decision_answer
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        e = _eligible_flow_entry()
        e.update({"review_state": "done", "review_critique_artifact_id": "rc_1",
                  "curator_state": "done", "decision_state": "awaiting_human",
                  "decision_options": ["proceed", "revise", "redirect_upstream",
                                       "abort", "edit"]})
        state.hook_state["pending_post_node_flow"] = [e]
        record_decision_answer(
            state, {"type": "decision_package", "producing_run_id": "r_prod_ok"}, "1")
        assert state.hook_state["pending_post_node_flow"] == []   # 走完 → 出列
        res = await execute_tool("run_node", state, node_type="experiment", user_note="测试派发",
                                 node_inputs={"x": 1})
        # 不该被 post-node flow gate 拦（可能因别的原因失败）
        if res.get("status") == "error":
            assert "post-producing flow 还没走完" not in res.get("error", "")


def test_human_revise_keeps_flow_until_replacement_exists():
    """REVISE is an authorized transition, not a completed flow."""
    from shared.tools.library.decision_package import record_decision_answer

    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        entry = _eligible_flow_entry()
        entry.update(
            {
                "review_state": "done",
                "review_critique_artifact_id": "review_critique__r1",
                "curator_state": "done",
                "decision_state": "awaiting_human",
                "decision_options": [
                    "proceed",
                    "revise",
                    "redirect_upstream",
                    "abort",
                    "edit",
                ],
            }
        )
        state.hook_state["pending_post_node_flow"] = [entry]
        out = record_decision_answer(
            state,
            {
                "type": "decision_package",
                "producing_run_id": "r_prod_ok",
                "recommended_feedback": "repair evidence lineage",
            },
            "2",
        )
        assert out["accepted_action"] == "revise"
        assert out["decision_state"] == "action_authorized"
        assert out["authorized_target_node"] == "hypothesis"
        assert len(state.hook_state["pending_post_node_flow"]) == 1


def test_redirect_without_target_stays_fail_closed():
    """A manual REDIRECT choice cannot create target=None permanent state."""
    from shared.tools.library.decision_package import record_decision_answer

    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        entry = _eligible_flow_entry()
        entry.update(
            {
                "review_state": "done",
                "review_critique_artifact_id": "review_critique__r1",
                "curator_state": "done",
                "decision_state": "awaiting_human",
                "decision_options": [
                    "proceed",
                    "revise",
                    "redirect_upstream",
                    "abort",
                    "edit",
                ],
            }
        )
        state.hook_state["pending_post_node_flow"] = [entry]
        out = record_decision_answer(
            state,
            {"type": "decision_package", "producing_run_id": "r_prod_ok"},
            "3",
        )
        assert out["decision_state"] == "awaiting_human"
        assert out["accepted_action"] is None
        # 断在**要点**上，不逐字比对文案 —— 文案改一个字就红的断言，
        # 保护的是字符串不是行为。
        _err = out["decision_validation_error"]
        assert "recommended_target_node" in _err and "REDIRECT" in _err
        assert len(state.hook_state["pending_post_node_flow"]) == 1


def test_interrupted_authorized_action_is_retryable_after_restart():
    """A process restart cannot leave continuous mode waiting on a dead child."""
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        entry = _eligible_flow_entry()
        entry.update(
            {
                "review_state": "done",
                "review_critique_artifact_id": "review_critique__r1",
                "curator_state": "done",
                "decision_state": "action_in_progress",
                "authorized_action": "revise",
                "authorized_target_node": "hypothesis",
                "action_attempt_count": 2,
            }
        )
        state.hook_state["pending_post_node_flow"] = [entry]
        assert recover_interrupted_decision_actions(state) == 1
        assert entry["decision_state"] == "action_authorized"
        assert entry["authorized_target_node"] == "hypothesis"
        assert entry["action_attempt_count"] == 2
        assert "interrupted" in entry["action_last_failure"]
        # Idempotent on subsequent calls in the same resumed process.
        assert recover_interrupted_decision_actions(state) == 0


def test_completed_revision_replaces_old_flow_with_fresh_review_flow():
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        old = _eligible_flow_entry()
        old.update(
            {
                "review_state": "done",
                "review_critique_artifact_id": "review_critique__r1",
                "curator_state": "done",
                "decision_state": "action_in_progress",
                "accepted_action": "revise",
                "authorized_action": "revise",
                "authorized_target_node": "hypothesis",
            }
        )
        state.hook_state["pending_post_node_flow"] = [old]
        child_dir = state.root / "revised_hypothesis"
        _child_record(child_dir, node_type="hypothesis", run_id="r_revised",
                      artifact_type="pre_registration", name="revised",
                      content="revised preregistration",
                      created_at="2026-07-22T00:00:00Z")
        summary = {
            "run_id": "r_revised",
            "node_type": "hypothesis",
            "status": "completed",
            "turns": 2,
            "state_dir": str(child_dir),
            "artifacts": [
                {
                    "id": "pre_registration__revised",
                    "type": "pre_registration",
                    "name": "revised",
                }
            ],
        }
        _finish_child(
            state,
            "hypothesis",
            {},
            summary,
            NodeHarness(
                node_type="hypothesis",
                required_output_artifact_types=["pre_registration"],
            ),
            None,
        )
        flow = state.hook_state["pending_post_node_flow"]
        assert len(flow) == 1
        assert flow[0]["producing_run_id"] == "r_revised"
        assert flow[0]["review_state"] == "pending"
        assert flow[0]["decision_state"] == "pending"


def test_failed_revision_returns_to_authorized_retry_state():
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        entry = _eligible_flow_entry()
        entry.update(
            {
                "decision_state": "action_in_progress",
                "authorized_action": "revise",
                "authorized_target_node": "hypothesis",
            }
        )
        state.hook_state["pending_post_node_flow"] = [entry]
        summary = {
            "run_id": "r_failed_revision",
            "node_type": "hypothesis",
            "status": "incomplete",
            "turns": 1,
            "state_dir": str(state.root / "missing-child"),
            "artifacts": [],
        }
        _finish_child(
            state,
            "hypothesis",
            {},
            summary,
            NodeHarness(
                node_type="hypothesis",
                required_output_artifact_types=["pre_registration"],
            ),
            None,
        )
        assert entry["decision_state"] == "action_authorized"
        assert "status='incomplete'" in entry["action_last_failure"]


def test_test_fixture_artifact_is_quarantined_from_production_parent():
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        child_dir = state.root / "fixture-child"
        _child_record(child_dir, node_type="writing", run_id="r_fixture",
                      artifact_type="manuscript", name="fixture",
                      content="domain-specific fixture text",
                      metadata={"_test_fixture_origin": "writing_compact_full_delivery"},
                      created_at="2026-07-22T00:00:00Z")
        summary = {
            "run_id": "r_fixture",
            "node_type": "writing",
            "status": "completed",
            "turns": 1,
            "state_dir": str(child_dir),
            "artifacts": [
                {
                    "id": "manuscript__fixture",
                    "type": "manuscript",
                    "name": "fixture",
                }
            ],
        }
        result = _finish_child(
            state,
            "writing",
            {},
            summary,
            NodeHarness(
                node_type="writing",
                required_output_artifact_types=["manuscript"],
            ),
            None,
        )
        assert result["imported_artifacts"] == []
        assert state.list_artifacts("manuscript") == []
        assert state.hook_state.get("pending_post_node_flow", []) == []


# ── C：retry 成功 → 必须重走 curator + decision ────────────────────────────

def test_retry_success_resets_the_decision_chain():
    """C（qinp #155 现象 4）：retry 拿到新 critique → 旧 curator/decision 作废，
    重置为 pending，必须基于新 critique 重走。"""
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        entry = _failed_review_entry(review_state="retry_authorized",
                                     curator_state="done", decision_state="done",
                                     accepted_action="retry_reviewer")
        state.hook_state["pending_post_node_flow"] = [entry]
        cdir = state.root / "rev_ok"
        _child_record(cdir, node_type="_reviewer", run_id="r_rev_ok",
                      artifact_type="review_critique", name="new", content="{}",
                      created_at="2026-07-15T00:00:00Z")
        summary = {"run_id": "r_rev_ok", "node_type": "_reviewer",
                   "status": "completed", "turns": 4, "state_dir": str(cdir),
                   "artifacts": [{"id": "review_critique__new",
                                  "type": "review_critique", "name": "new"}]}
        from core.harness import NodeHarness
        _finish_child(state, "_reviewer",
                      {"source_node_type": "hypothesis",
                       "producer_run_id": "r_prod_ok"}, summary,
                      NodeHarness(node_type="_reviewer",
                                  required_output_artifact_types=["review_critique"]),
                      None)
        e = state.hook_state["pending_post_node_flow"][0]
        assert e["review_state"] == "done"
        assert e["review_critique_artifact_id"] == "review_critique__new"
        assert e["decision_state"] == "pending"    # ← 重置
        assert e["accepted_action"] is None
        assert e["review_attempt_count"] == 2


# ── D：ABORT 关闭 flow + 保留历史 ──────────────────────────────────────────

def test_human_abort_closes_flow_and_keeps_history():
    from shared.tools.library.decision_package import record_decision_answer
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        state.hook_state["pending_post_node_flow"] = [_failed_review_entry(
            decision_state="awaiting_human",
            decision_options=["retry_reviewer", "revise", "redirect_upstream",
                              "abort", "edit"])]
        out = record_decision_answer(
            state, {"type": "decision_package", "producing_run_id": "r_prod_ok"}, "4")
        assert out["accepted_action"] == "abort"
        assert out["review_state"] == "aborted"
        assert state.hook_state["pending_post_node_flow"] == []   # active flow 关闭
        assert out["decision_history"][-1]["chosen_action"] == "abort"


def test_unparsable_answer_keeps_flow_failclosed():
    """答复认不出 → 不放行任何东西，entry 留着继续拦（fail-closed）。"""
    from shared.tools.library.decision_package import record_decision_answer
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        state.hook_state["pending_post_node_flow"] = [_failed_review_entry(
            decision_state="awaiting_human",
            decision_options=["retry_reviewer", "revise", "redirect_upstream",
                              "abort", "edit"])]
        out = record_decision_answer(
            state, {"type": "decision_package", "producing_run_id": "r_prod_ok"},
            "呃我再想想")
        assert out["accepted_action"] is None
        assert out["review_state"] == "failed_awaiting_human"     # 没变
        assert len(state.hook_state["pending_post_node_flow"]) == 1


@pytest.mark.asyncio
async def test_services_are_never_blocked_by_the_review_gate():
    """v2.0 架构不变量：服务节点（post_run_flow: none）不受 post-node 审查门约束。

    literature / data / postprocess 由消费方（analysis / experiment / writing）
    在自己的 run 内同步调用。若它们被"上一个 producing 的 review 没走完"卡住，
    消费方就永远拿不到数据/文献/图 —— 服务等于不可用。这条与
    test_downstream_blocked_while_review_not_done 是一对：producing 必须拦，
    服务必须放。
    """
    from core.loader import load_harness
    with tempfile.TemporaryDirectory() as td:
        state = _orch_state(Path(td))
        state.hook_state["_callable_nodes"] = ["*"]
        state.hook_state["pending_post_node_flow"] = [{
            "producing_node": "hypothesis", "producing_run_id": "r_block",
            "artifact_ids": ["pre_registration__x"],
            "review_state": "pending", "curator_state": "pending",
            "decision_state": "pending",
        }]
        for service in ("literature", "data", "postprocess"):
            assert load_harness(service).is_service, f"{service} 应当是服务节点"
            res = await execute_tool(
                "run_node", state, node_type=service, user_note="测试派发", node_inputs={"spec": "x"}
            )
            err = str(res.get("error") or "")
            blocked = res.get("status") == "error" and (
                "present_decision_package" in err or "review" in err.lower()
            )
            assert not blocked, f"服务 {service} 被审查门拦住：{err}"


