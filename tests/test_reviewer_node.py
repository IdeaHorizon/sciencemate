"""v0.4 _reviewer + post-producing flow tests.

涉及：
  - _reviewer harness 能 load + 含必要工具
  - present_decision_package 工具能渲染 + 生成 pause event
  - post_node_review_flow_reminder hook 按 3 阶段渐进
  - skip_post_node_review opt-out 字段被 loader 读对
  - auto_approve toggle 工作
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.artifact_provenance import forwarded, produced
from core.bootstrap import bootstrap

bootstrap()

from core.state import State                                      # noqa: E402
from core.harness import NodeHarness                              # noqa: E402
from core.loader import load_harness                              # noqa: E402
from core.tool_registry import get_tool                           # noqa: E402


def make_state(td: Path, node_type: str = "_orchestrator") -> State:
    os.environ["HARNESS_FRAMEWORK_HOME"] = str(td)
    return State.new(node_type=node_type, base_dir=td)


def _review_provenance(state: State) -> dict:
    return forwarded(
        produced("_reviewer", "review-run"),
        via_node_type=state.node_type,
        via_run_id=state.run_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# _reviewer harness
# ─────────────────────────────────────────────────────────────────────────────

# test_reviewer_harness_loads 已随 QC 层删除（2026-08-22）：改为下方新断言

def test_reviewer_hard_blocks_figureless_publication_manuscripts():
    h = load_harness("_reviewer")
    assert "投稿型研究论文没有科研图片" in h.system_prompt
    spec = Path("nodes/writing/review_spec.md").read_text(encoding="utf-8")
    assert "hard_lines_passed" in spec and "critical concern" in spec
    rules_text = " ".join(h.rules)
    assert "缺图 → critical" in rules_text
    assert "绝不 proceed" in rules_text


def test_reviewer_in_kb_heuristic_injection():
    """_reviewer 跟 _curator 一样：system_prompt 自动注入 KB 启发式 rule"""
    from core.context_engine import build_messages, _should_inject_kb_heuristic
    assert _should_inject_kb_heuristic("_reviewer")
    assert _should_inject_kb_heuristic("_curator")
    assert not _should_inject_kb_heuristic("_orchestrator")
    print("  ✓ _reviewer 在 KB heuristic injection 白名单内")


# ─────────────────────────────────────────────────────────────────────────────
# present_decision_package 工具
# ─────────────────────────────────────────────────────────────────────────────

def test_present_decision_package_tool_registered():
    t = get_tool("present_decision_package")
    assert t is not None
    print("  ✓ present_decision_package tool registered")


def test_decision_package_render_with_review():
    from shared.tools.library.decision_package import _render_decision_package
    text = _render_decision_package(
        source_node_type="hypothesis",
        producing_run_id="r_abc",
        producing_summary="12 turns",
        artifact_ids_produced=["pre_registration__test"],
        curator_summary="+3 claims",
        review_critique_json={
            "verdict": "approve_with_revisions",
            "confidence": 0.7,
            "strengths": ["criteria 量化"],
            "concerns": [{"severity": "major", "description": "N power 不够"}],
            "recommended_action": {"action": "revise", "feedback_to_next_run": "Add power justification"},
        },
        review_failed_reason=None,
        offer=_offer_for_test("revise"),
        recommended_feedback="Add power justification",
    )
    assert "approve_with_revisions" in text
    assert "← recommended" in text
    # v0.5 起 5 个 option
    for i in (1, 2, 3, 4, 5):
        assert f"[{i}]" in text, f"missing option [{i}]"
    assert "REDIRECT" in text     # v0.5 新增 [3]
    assert "N power 不够" in text
    print("  ✓ decision package 渲染 standard 路径 OK")


def test_decision_package_render_review_failed():
    from shared.tools.library.decision_package import _render_decision_package
    text = _render_decision_package(
        source_node_type="experiment",
        producing_run_id="r_exp",
        producing_summary="",
        artifact_ids_produced=["experiment_log__xx"],
        curator_summary="+1 claim",
        review_critique_json=None,
        review_failed_reason="LLM timeout after 3 retries",
        offer=_offer_for_test("proceed"),
        recommended_feedback="",
    )
    # 2026-07-09（架构修复 C）：review 失败改 fail-closed —— 不再 "proceeding
    # without review"（默认 PROCEED），而是默认推荐 REVISE，不在无质量信号时放行。
    assert "review 失败" in text
    assert "LLM timeout" in text
    assert "fail-closed" in text and "REVISE" in text
    assert "proceeding without review" not in text
    print("  ✓ decision package review-failed 现在 fail-closed（默认 REVISE）")


def test_decision_package_render_review_skipped():
    from shared.tools.library.decision_package import _render_decision_package
    text = _render_decision_package(
        source_node_type="data",
        producing_run_id="r_data",
        producing_summary="",
        artifact_ids_produced=["dataset__lj"],
        curator_summary="+0 KB changes",
        review_critique_json=None,
        review_failed_reason=None,
        offer=_offer_for_test("proceed"),
        recommended_feedback="",
    )
    assert "skipped" in text and "owner opted out" in text
    print("  ✓ decision package 渲染 owner-skip 路径 OK")


def test_decision_package_recommended_index_from_action():
    from shared.tools.library.decision_package import _recommended_index_from_action
    # v0.5：5 个 option（新增 redirect_upstream 在 index 2）
    assert _recommended_index_from_action("proceed") == 0
    assert _recommended_index_from_action("revise") == 1
    assert _recommended_index_from_action("redirect_upstream") == 2
    assert _recommended_index_from_action("abort") == 3
    assert _recommended_index_from_action("escalate_to_human") == 4
    assert _recommended_index_from_action("unknown") == 0     # fallback
    print("  ✓ recommended_action → option index 映射 OK (v0.5 5-option)")


# ─────────────────────────────────────────────────────────────────────────────
# v0.5: redirect_upstream
# ─────────────────────────────────────────────────────────────────────────────


def test_decision_package_render_with_redirect_upstream():
    """reviewer 推荐 redirect_upstream → option [3] 标 recommended + 显示
    具体上游节点名 + 显示 'Focused query for upstream' 而非 'Feedback for re-run'。
    """
    from shared.tools.library.decision_package import _render_decision_package
    text = _render_decision_package(
        source_node_type="hypothesis",
        producing_run_id="r_hyp",
        producing_summary="12 turns",
        artifact_ids_produced=["pre_registration__h1", "research_plan__h1"],
        curator_summary="+2 claims",
        review_critique_json={
            "verdict": "major_concerns",
            "confidence": 0.8,
            "strengths": ["criteria 量化"],
            "concerns": [{"severity": "critical",
                          "description": "baseline 选择反复闪烁，根因是 literature 没覆盖 X"}],
            "recommended_action": {
                "action": "redirect_upstream",
                "target_node": "literature",
                "feedback_to_next_run": "补充 X 这一组 baseline 在 Y 条件下的对比",
                "rationale": "hypothesis 已尽力；信息缺口在上游",
            },
        },
        review_failed_reason=None,
        offer=_offer_for_test("redirect_upstream", target="literature"),  # redirect_upstream → option [3]
        recommended_feedback="补充 X 这一组 baseline 在 Y 条件下的对比",
        recommended_target_node="literature",
    )
    # 5 个 option
    for i in (1, 2, 3, 4, 5):
        assert f"[{i}]" in text
    # [3] 推荐 marker + 具体目标节点名
    assert "[3] REDIRECT to upstream → 'literature'" in text
    assert "← recommended" in text
    # 文案区分 redirect vs revise
    assert "Focused query for upstream" in text
    assert "Feedback for re-run" not in text
    # 上游 target 出现在 "Recommended:" 行
    assert "Recommended: REDIRECT to upstream → 'literature'" in text
    print("  ✓ decision package 渲染 redirect_upstream 路径 OK")


@pytest.mark.asyncio
async def test_present_decision_package_redirect_upstream_metadata():
    """完整调 present_decision_package：reviewer 写 redirect_upstream → pause
    metadata 含 recommended_target_node + recommended_option_index=2。
    这是 orchestrator 决定如何 route 的关键数据。
    """
    import json as _json
    import pytest as _pytest
    import tempfile
    from pathlib import Path
    from core.state import State
    from core.tool_registry import execute as execute_tool
    from core.bootstrap import bootstrap as _bootstrap
    _bootstrap()
    with tempfile.TemporaryDirectory() as td:
        state = State.new(node_type="_orchestrator", base_dir=Path(td),
                            project_id="p_redirect")
        # 造一个 reviewer 输出 artifact
        critique = {
            "verdict": "major_concerns",
            "confidence": 0.8,
            "concerns": [],
            "strengths": [],
            "recommended_action": {
                "action": "redirect_upstream",
                "target_node": "literature",
                "feedback_to_next_run": "补 X 主题",
                "rationale": "信息缺口在上游",
            },
        }
        art = state.save_artifact(
            "review_critique", "test_critique",
            _json.dumps(critique),
            {"produced_by_node_type": "_reviewer"},
            provenance=_review_provenance(state),
        )
        res = await execute_tool(
            "present_decision_package", state,
            source_node_type="hypothesis",
            producing_run_id="r_test",
            review_critique_artifact_id=art["id"],
        )
        assert res["status"] == "pause"
        pe = res["pause_event"]
        md = pe["metadata"]
        # recommended 是**呈递的**事实，在 pause_event 顶层（Offer 投影摊开），
        # 不再在 metadata 里另存一份。
        #
        # 2026-09-17：这条原来断言"推荐 REDIRECT 到 literature"。literature 后来
        # 改成了服务节点（`post_run_flow: none`）—— 退回它，那条审查义务永远关不掉，
        # 而空转熔断在这一档看不见（yuankk 实测空转 40 轮）。所以框架**撤下这个推荐**，
        # 但绝不静默：reviewer 的诊断与它点名的目标一个字不丢，REDIRECT 也仍在菜单里
        # （人可以自己指一个合法上游）。
        assert pe["recommended_choice_id"] == "revise"
        assert md["recommended_action"] == "revise"
        assert not md["recommended_target_node"]
        assert "补 X 主题" in md["recommended_feedback"], "reviewer 的反馈不许丢"
        assert "literature" in md["recommended_feedback"], "它点名的目标也不许丢"
        assert "框架撤下 REDIRECT 推荐" in pe["context"], "撤下必须写在脸上，不许静默"
        assert any("REDIRECT" in opt for opt in pe["options"]), "菜单里仍要有 REDIRECT"


# 2026-08-19：`test_run_node_hard_gates_producing_when_pending_decision` 随
# 「flow 没闭合不许起新 producing 节点」那道墙一起删除。它钉的是墙的拦截行为，
# 而墙防的窗口已经不存在（见 tests/test_orchestrator_never_holds_an_open_flow.py）。

@pytest.mark.asyncio
async def test_run_node_allows_reviewer_curator_with_pending_decision():
    """v0.5.1 hard gate 不卡 _reviewer / _curator —— 它们正是用来推进
    pending entry 的（review_state / curator_state 翻 done 靠它们）。"""
    import tempfile
    from pathlib import Path
    from core.state import State
    from core.tool_registry import execute as execute_tool
    from core.bootstrap import bootstrap as _bootstrap
    _bootstrap()
    with tempfile.TemporaryDirectory() as td:
        state = State.new(node_type="_orchestrator", base_dir=Path(td),
                            project_id="p_gate2")
        state.hook_state["_callable_nodes"] = ["*"]
        # 同样 pending decision，但调 _reviewer 应该 ok（只是会因为别的原因
        # 失败 —— 比如 _reviewer harness 不存在某依赖；我们只看不被 hard gate
        # 卡住，所以检查不是因为 hard gate 报错）
        state.hook_state["pending_post_node_flow"] = [{
            "producing_node": "literature",
            "producing_run_id": "r_lit_1",
            "artifact_ids": ["x"],
            "review_state": "pending",
            "curator_state": "pending",
            "decision_state": "pending",
        }]
        res = await execute_tool(
            "run_node", state,
            node_type="_reviewer", user_note="测试派发",
            node_inputs={"artifact_id": "x", "source_node_type": "literature",
                         "producer_run_id": "r_lit_1"},
        )
        # 不该是 hard gate error；可能是其它 error / 也可能 success
        if res.get("status") == "error":
            assert "present_decision_package" not in res.get("error", ""), (
                f"_reviewer 不该被 hard gate 卡：{res.get('error')}"
            )


@pytest.mark.asyncio
async def test_run_node_unblocked_after_decision_clears_entry():
    """decision_state=done 后 entry 应该出 pending list（其它 state 也都
    done），然后 hard gate 不再阻挡。"""
    import tempfile
    from pathlib import Path
    from core.state import State
    from core.tool_registry import execute as execute_tool
    from core.bootstrap import bootstrap as _bootstrap
    _bootstrap()
    with tempfile.TemporaryDirectory() as td:
        state = State.new(node_type="_orchestrator", base_dir=Path(td),
                            project_id="p_gate3")
        state.hook_state["_callable_nodes"] = ["*"]
        # 全 done → entry 应被 present_decision_package 清掉；模拟"已清"状态
        state.hook_state["pending_post_node_flow"] = []
        res = await execute_tool(
            "run_node", state,
            node_type="literature", user_note="测试派发",
            node_inputs={"research_question": "test"},
        )
        # 不该被 hard gate 卡；可能其它原因失败
        if res.get("status") == "error":
            assert "present_decision_package" not in res.get("error", ""), (
                f"pending 清空后 hard gate 不该卡：{res.get('error')}"
            )


@pytest.mark.asyncio
async def test_redirect_upstream_missing_target_degrades_to_revise():
    """reviewer 写 redirect_upstream 但漏 target_node → **不得降级为 revise**。

    v3.6 行为变更（本测试原先固化的是 bug）：旧实现把它静默改判 revise，等于
    因为一个 schema 疏漏抹掉"根因在上游"的诊断，然后继续重跑一个修不好的节点。
    E2E#1 实测代价：reviewer 建议 redirect 5 次 → 实际执行 0 次，74 次全是 revise。
    新行为：依赖图能唯一确定上游就自动补全并保留 REDIRECT（hypothesis 的上游
    只有 literature）；有歧义则转 retry_reviewer 要求指名，仍然不退化成 revise。
    """
    import json as _json
    import tempfile
    from pathlib import Path
    from core.state import State
    from core.tool_registry import execute as execute_tool
    from core.bootstrap import bootstrap as _bootstrap
    _bootstrap()
    with tempfile.TemporaryDirectory() as td:
        state = State.new(node_type="_orchestrator", base_dir=Path(td),
                            project_id="p_bad")
        critique = {
            "verdict": "major_concerns",
            "concerns": [], "strengths": [],
            "recommended_action": {
                "action": "redirect_upstream",
                # 故意漏 target_node
                "feedback_to_next_run": "fix it",
            },
        }
        art = state.save_artifact(
            "review_critique", "bad_critique", _json.dumps(critique),
            {"produced_by_node_type": "_reviewer"},
            provenance=_review_provenance(state),
        )
        res = await execute_tool(
            "present_decision_package", state,
            source_node_type="hypothesis",
            producing_run_id="r_bad",
            review_critique_artifact_id=art["id"],
        )
        md = res["pause_event"]["metadata"]
        # hypothesis 唯一的依赖图上游 literature 后来改成了服务节点，退回它这条
        # 义务永远关不掉 → 候选集为空，补不出来。此时**仍然保留 REDIRECT 意图**，
        # 让人看见"根因在上游"这个诊断，由人决定改 revise 还是 abort。
        assert md["recommended_action"] == "redirect_upstream", (
            "绝不能降级为 revise —— 那会抹掉'根因在上游'的诊断并继续重跑修不好的节点"
        )
        assert not md.get("recommended_target_node"), (
            "补出了一个目标？那它必须是能关掉 flow 的节点"
        )


# ─────────────────────────────────────────────────────────────────────────────
# post_node_review_flow_reminder hook
# ─────────────────────────────────────────────────────────────────────────────

def test_hook_no_pending_no_inject():
    from core.loop_hooks_builtin import _post_node_review_flow_reminder_on_turn_start
    from core.loop_hooks import HookContext
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        ctx = HookContext(
            harness=NodeHarness(node_type="_orchestrator"),
            state=state, messages=[], turn=1,
        )
        result = _post_node_review_flow_reminder_on_turn_start(ctx)
        assert result is None
        print("  ✓ pending 空 → 不注入")


def test_hook_is_silent_while_the_runtime_drives_the_flow():
    """Move 1d 之后：flow 的常规两步由运行时走完，提醒 hook **不该说话**。

    此前它每轮把下一步调用连参数打印给调度器照抄（"NEXT: Step 2 — call _reviewer"）。
    手续归运行时之后，再提醒就是让它去做一件已经做完的事 —— 换来一次重复呈递。

    hook 只在**真需要调度器动手**的状态说话：人选了 REVISE/REDIRECT 要起指定节点，
    人选了 EDIT 要等人改完，以及进程重启丢了 pause 需要重新呈递。
    """
    from core.loop_hooks_builtin import _post_node_review_flow_reminder_on_turn_start
    from core.loop_hooks import HookContext
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        entry = {
            "producing_node": "hypothesis",
            "producing_run_id": "r_hyp",
            "artifact_ids": ["pre_registration__t1"],
            "at": "r_hyp",
            "review_state": "pending",
            "review_critique_artifact_id": None,
            "review_failed_reason": None,
            "decision_state": "pending",
        }
        state.hook_state["pending_post_node_flow"] = [entry]
        ctx = HookContext(
            harness=NodeHarness(node_type="_orchestrator"),
            state=state, messages=[], turn=1,
        )

        # review 待办 / 决策待呈递 —— 两者都是运行时的活，不提醒调度器。
        for state_name in ("pending", "done"):
            entry["review_state"] = state_name
            if state_name == "done":
                entry["review_critique_artifact_id"] = "review_critique__xx"
            out = _post_node_review_flow_reminder_on_turn_start(ctx)
            text = out[0].content if out else ""
            assert "call _reviewer" not in text, "运行时自己派 reviewer，别再教调度器抄"
            assert "call present_decision_package" not in text, "呈递也归运行时"

        # 人选了 REVISE → 起指定节点，这一步运行时替不了（是人的决定的执行面）
        entry["decision_state"] = "action_authorized"
        entry["authorized_action"] = "revise"
        entry["authorized_target_node"] = "hypothesis"
        r = _post_node_review_flow_reminder_on_turn_start(ctx)
        assert r and "execute the authorized review action" in r[0].content
        assert "'hypothesis'" in r[0].content

        # 进程重启丢了 pause → 需要重新呈递，这条也只有账本知道
        entry["decision_state"] = "awaiting_human"
        r = _post_node_review_flow_reminder_on_turn_start(ctx)
        assert r and "重新呈递决策包" in r[0].content

        # 全 done → 不注入
        entry["decision_state"] = "done"
        assert _post_node_review_flow_reminder_on_turn_start(ctx) is None


def test_hook_re_presents_stale_awaiting_decision_after_restart():
    """Persisted flow remains actionable after the in-memory pause is lost."""
    from core.loop_hooks_builtin import _post_node_review_flow_reminder_on_turn_start
    from core.loop_hooks import HookContext
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        state.hook_state["pending_post_node_flow"] = [{
            "producing_node": "writing",
            "producing_run_id": "r-writing",
            "artifact_ids": ["manuscript__draft"],
            "review_state": "done",
            "review_critique_artifact_id": "review_critique__writing",
            "curator_state": "done",
            "decision_state": "awaiting_human",
        }]
        ctx = HookContext(
            harness=NodeHarness(node_type="_orchestrator"),
            state=state, messages=[], turn=1,
        )

        result = _post_node_review_flow_reminder_on_turn_start(ctx)

        assert result is not None
        assert "重新呈递决策包" in result[0].content
        assert "r-writing" in result[0].content
        assert "present_decision_package" in result[0].content


def test_hook_review_skipped_path():
    """reviewer 被 owner 跳过

    Move 1d 之后：review 无论 skipped 还是 failed，下一步（呈递决策包）都归运行时，
    所以 hook 在这两个状态下**不注入** —— 状态本身仍如实记在 flow entry 里，
    决策包会带着 review_failed_reason 呈递，人看得到。
    """
    from core.loop_hooks_builtin import _post_node_review_flow_reminder_on_turn_start
    from core.loop_hooks import HookContext
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        state.hook_state["pending_post_node_flow"] = [{
            "producing_node": "hypothesis",
            "producing_run_id": "r_hyp",
            "artifact_ids": ["pre_registration__t1"],
            "at": "r_hyp",
            "review_state": 'skipped',
            "review_failed_reason": None,
            "decision_state": "pending",
        }]
        ctx = HookContext(
            harness=NodeHarness(node_type="_orchestrator"),
            state=state, messages=[], turn=1,
        )
        assert _post_node_review_flow_reminder_on_turn_start(ctx) is None


def test_hook_review_failed_path():
    """reviewer 失败

    Move 1d 之后：review 无论 skipped 还是 failed，下一步（呈递决策包）都归运行时，
    所以 hook 在这两个状态下**不注入** —— 状态本身仍如实记在 flow entry 里，
    决策包会带着 review_failed_reason 呈递，人看得到。
    """
    from core.loop_hooks_builtin import _post_node_review_flow_reminder_on_turn_start
    from core.loop_hooks import HookContext
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        state.hook_state["pending_post_node_flow"] = [{
            "producing_node": "hypothesis",
            "producing_run_id": "r_hyp",
            "artifact_ids": ["pre_registration__t1"],
            "at": "r_hyp",
            "review_state": 'failed',
            "review_failed_reason": 'reviewer 子 run 没产出可解析 critique',
            "decision_state": "pending",
        }]
        ctx = HookContext(
            harness=NodeHarness(node_type="_orchestrator"),
            state=state, messages=[], turn=1,
        )
        assert _post_node_review_flow_reminder_on_turn_start(ctx) is None


# ─────────────────────────────────────────────────────────────────────────────
# skip_post_node_review 字段
# ─────────────────────────────────────────────────────────────────────────────

def test_skip_post_node_review_default_false():
    for n in ("hypothesis", "experiment", "writing", "_curator", "_reviewer"):
        h = load_harness(n)
        assert h.skip_post_node_review is False, f"{n} 默认应为 False"
    print("  ✓ 所有节点默认 skip_post_node_review=False")


# ─────────────────────────────────────────────────────────────────────────────
# auto_approve toggle
# ─────────────────────────────────────────────────────────────────────────────

def test_auto_approve_toggle():
    from core import pause_driver
    pause_driver.set_auto_approve(False)
    assert pause_driver.AUTO_APPROVE_ENABLED is False
    pause_driver.set_auto_approve(True, countdown_sec=3)
    assert pause_driver.AUTO_APPROVE_ENABLED is True
    assert pause_driver.AUTO_APPROVE_COUNTDOWN_SEC == 3
    pause_driver.set_auto_approve(False)
    assert pause_driver.AUTO_APPROVE_ENABLED is False
    print("  ✓ auto_approve toggle 工作 (set_auto_approve + 全局变量)")


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("== v0.4 _reviewer + post-producing flow tests ==\n")
    tests = [
        test_reviewer_harness_loads,
        test_reviewer_in_kb_heuristic_injection,
        test_present_decision_package_tool_registered,
        test_decision_package_render_with_review,
        test_decision_package_render_review_failed,
        test_decision_package_render_review_skipped,
        test_decision_package_recommended_index_from_action,
        test_hook_no_pending_no_inject,
        test_hook_progression_through_3_steps,
        test_hook_review_skipped_path,
        test_hook_review_failed_path,
        test_skip_post_node_review_default_false,
        test_auto_approve_toggle,
    ]
    for t in tests:
        print(f"- {t.__name__}")
        t()
    print(f"\nALL {len(tests)} TESTS PASS ✓")


def _offer_for_test(action="proceed", *, review_failed=False, target=None):
    """渲染器的选项集来自 offer（一处声明），测试也走同一条路。"""
    from shared.tools.library.decision_package import (
        _NORMAL_ACTIONS, _REVIEW_FAILED_ACTIONS, build_decision_offer,
    )
    ids = list(_REVIEW_FAILED_ACTIONS if review_failed else _NORMAL_ACTIONS)
    return build_decision_offer(
        decision_id="r_test:ptest", source_node_type="hypothesis",
        action_ids=ids, recommended_action=action, redirect_target_node=target,
    )
