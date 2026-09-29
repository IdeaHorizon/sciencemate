"""高危命令拦截（2026-07）：run_bash / execute_python 的 bypass / 普通两模式。

覆盖：
  - 危险模式检测（shell + python 两口径）
  - 普通模式：命中 → pause，人拒绝/无回应 → 仍挡；人批准 → 一次性放行
  - bypass 模式：完全不检查
  - 非危险命令两种模式下都正常跑（不误伤）
"""
from __future__ import annotations

import pytest

from core.state import State
from shared.lib import dangerous_commands as dc


@pytest.fixture(autouse=True)
def _reset_bypass():
    dc.set_bypass_mode(False)
    yield
    dc.set_bypass_mode(False)


# ── 危险模式检测 ─────────────────────────────────────────────────────────────

def test_match_high_risk_shell_positive():
    assert dc.match_high_risk("rm -rf /home/user/data", mode="shell")
    assert dc.match_high_risk("sudo rm important.txt", mode="shell")
    assert dc.match_high_risk("git push origin main --force", mode="shell")
    assert dc.match_high_risk("dd if=/dev/zero of=/dev/sda", mode="shell")


def test_match_high_risk_shell_negative():
    assert dc.match_high_risk("ls -la", mode="shell") is None
    assert dc.match_high_risk("python train.py --epochs 10", mode="shell") is None
    assert dc.match_high_risk("rm output.tmp", mode="shell") is None   # 非 -rf 的单文件删除不算


def test_match_high_risk_python_positive():
    assert dc.match_high_risk("import shutil; shutil.rmtree('/data')", mode="python")
    assert dc.match_high_risk("import os; os.system('rm -rf /')", mode="python")


def test_match_high_risk_python_negative():
    assert dc.match_high_risk("import pandas as pd; pd.read_csv('x.csv')", mode="python") is None


# ── 确认标记 一次性语义 ──────────────────────────────────────────────────────

def test_confirmation_is_one_shot(tmp_path):
    state = State.new(node_type="t", base_dir=tmp_path)
    cmd = "rm -rf /tmp/scratch"
    assert not dc.is_confirmed(state, cmd)
    dc.mark_confirmed(state, cmd)
    assert dc.is_confirmed(state, cmd)
    dc.consume_confirmation(state, cmd)
    assert not dc.is_confirmed(state, cmd)   # 用掉就没了，同命令再来要重新问


def test_looks_like_approval():
    assert dc.looks_like_approval("批准执行")
    assert dc.looks_like_approval("同意，去吧")
    assert dc.looks_like_approval("approve")
    assert dc.looks_like_approval("yes")
    assert not dc.looks_like_approval("不要")
    assert not dc.looks_like_approval("拒绝")
    assert not dc.looks_like_approval("")


# ── run_bash 集成：普通模式 pause / bypass 直跑 / 安全命令不误伤 ─────────────

# 注意：这里直接调底层函数（`_run_bash` / `_execute_python`），不经
# `core.tool_registry.execute("run_bash"/"execute_python", ...)`。原因：
# bootstrap() 会无条件 import 所有节点，其中 experiment 的 safe_bash.py 仍在
# framework_exemptions.yaml 的迁移豁免期内同名覆盖 run_bash/execute_python
# （deadline 2026-08-01）——若走全局 registry，命中的会是 experiment 那套独立
# 的旧机制（env var 授权、返 error 不 pause），而不是这里新增的框架版实现。
# ⚠️ 这也意味着：**在完整 bootstrap 的真实进程里，只要 experiment 节点的
# 工具被加载，run_bash/execute_python 就会被它的重名覆盖遮蔽，这里新增的
# bypass/普通模式暂时不生效** —— 这是已跟踪的独立问题（tool_name_collisions
# 豁免表），8-01 后 lujy 把 safe_bash.py 的覆盖改名，框架版本才会在全流程里
# 真正生效。这些测试验证的是框架版函数本身的逻辑正确。

@pytest.mark.asyncio
async def test_run_bash_normal_mode_pauses_on_highrisk(tmp_path):
    from shared.tools.builtin import _run_bash
    dc.set_bypass_mode(False)
    state = State.new(node_type="t", base_dir=tmp_path)
    res = await _run_bash(state, cmd="rm -rf /tmp/should_not_run_xyz")
    assert res.get("status") == "pause"
    assert "pause_event" in res
    assert "批准" in res["pause_event"]["question"] or "高危" in res["pause_event"]["question"]


@pytest.mark.asyncio
async def test_run_bash_bypass_mode_runs_directly(tmp_path):
    from shared.tools.builtin import _run_bash
    dc.set_bypass_mode(True)
    state = State.new(node_type="t", base_dir=tmp_path)
    res = await _run_bash(state, cmd="echo would-be-dangerous-but-bypassed")
    assert res.get("status") == "success"
    assert "would-be-dangerous-but-bypassed" in res.get("stdout_tail", "")


@pytest.mark.asyncio
async def test_run_bash_confirmed_command_runs_once_then_reblocks(tmp_path):
    from shared.tools.builtin import _run_bash
    dc.set_bypass_mode(False)
    state = State.new(node_type="t", base_dir=tmp_path)
    # rm -rf 一个不存在的路径：命中危险模式，且真跑时 -f 让它安全成功（returncode 0）
    cmd = "rm -rf /tmp/hf_test_confirmed_run_xyz_nonexistent"
    # 未确认 → pause
    res1 = await _run_bash(state, cmd=cmd)
    assert res1.get("status") == "pause"
    # 人批准后（模拟 hook 已 mark_confirmed）→ 这次放行
    dc.mark_confirmed(state, cmd)
    res2 = await _run_bash(state, cmd=cmd)
    assert res2.get("status") == "success"
    # 一次性：同一命令再跑一次又要重新问
    res3 = await _run_bash(state, cmd=cmd)
    assert res3.get("status") == "pause"


@pytest.mark.asyncio
async def test_run_bash_safe_command_unaffected_in_normal_mode(tmp_path):
    """正向配套：非危险命令在普通模式下也必须直接跑，不误伤。"""
    from shared.tools.builtin import _run_bash
    dc.set_bypass_mode(False)
    state = State.new(node_type="t", base_dir=tmp_path)
    res = await _run_bash(state, cmd="echo hello-safe")
    assert res.get("status") == "success"
    assert "hello-safe" in res.get("stdout_tail", "")


@pytest.mark.asyncio
async def test_execute_python_normal_mode_pauses_on_highrisk(tmp_path):
    from shared.tools.library.python_exec import _execute_python
    dc.set_bypass_mode(False)
    state = State.new(node_type="t", base_dir=tmp_path)
    res = await _execute_python(
        state, code="import shutil\nshutil.rmtree('/tmp/should_not_be_removed_xyz')",
    )
    assert res.get("status") == "pause"


@pytest.mark.asyncio
async def test_execute_python_safe_code_unaffected(tmp_path):
    from shared.tools.library.python_exec import _execute_python
    dc.set_bypass_mode(False)
    state = State.new(node_type="t", base_dir=tmp_path)
    res = await _execute_python(state, code="print('hello-safe-python')")
    assert res.get("status") == "success"


# ── highrisk_confirm hook：读人回答 → 批准/拒绝 ──────────────────────────────

def test_highrisk_confirm_hook_marks_confirmed_on_approval(tmp_path):
    from core.loop_hooks_builtin import _highrisk_confirm_on_turn_start
    from core.loop_hooks import HookContext
    from core.llm import LLMMessage
    from core.harness import NodeHarness

    state = State.new(node_type="t", base_dir=tmp_path)
    cmd = "rm -rf /tmp/hooktest"
    dc.register_pending_ask(state, tool="run_bash", text=cmd, category="test")
    ctx = HookContext(
        harness=NodeHarness(node_type="t", system_prompt="s"),
        state=state, turn=2,
        messages=[
            LLMMessage(role="system", content="s"),
            LLMMessage(role="tool", tool_call_id="c1", name="run_bash", content="批准执行"),
        ],
    )
    _highrisk_confirm_on_turn_start(ctx)
    assert dc.is_confirmed(state, cmd)


def test_highrisk_confirm_hook_denial_does_not_confirm(tmp_path):
    from core.loop_hooks_builtin import _highrisk_confirm_on_turn_start
    from core.loop_hooks import HookContext
    from core.llm import LLMMessage
    from core.harness import NodeHarness

    state = State.new(node_type="t", base_dir=tmp_path)
    cmd = "rm -rf /tmp/hooktest2"
    dc.register_pending_ask(state, tool="run_bash", text=cmd, category="test")
    ctx = HookContext(
        harness=NodeHarness(node_type="t", system_prompt="s"),
        state=state, turn=2,
        messages=[
            LLMMessage(role="system", content="s"),
            LLMMessage(role="tool", tool_call_id="c1", name="run_bash", content="不要，太危险了"),
        ],
    )
    _highrisk_confirm_on_turn_start(ctx)
    assert not dc.is_confirmed(state, cmd)


def test_highrisk_confirm_hook_noop_when_nothing_pending(tmp_path):
    """正向配套：没有 pending ask 时 hook 什么都不做，不报错。"""
    from core.loop_hooks_builtin import _highrisk_confirm_on_turn_start
    from core.loop_hooks import HookContext
    from core.harness import NodeHarness

    state = State.new(node_type="t", base_dir=tmp_path)
    ctx = HookContext(
        harness=NodeHarness(node_type="t", system_prompt="s"),
        state=state, turn=1, messages=[],
    )
    assert _highrisk_confirm_on_turn_start(ctx) is None


def test_highrisk_confirm_is_always_on_hook():
    from core.agent_loop import _ALWAYS_ON_HOOKS
    assert "highrisk_confirm" in _ALWAYS_ON_HOOKS


# ── request_human_input：bypass 语义（issue #111）───────────────────────────
# bypass 之前只挡了 run_bash/execute_python 的高危拦截，request_human_input
# 仍然照常 pause——本应无人值守的 run（比如 experiment 提交 SLURM 作业前）
# 照样会卡住等人。修复：bypass 时不 pause，直接给 best-judgment 默认答案。

@pytest.mark.asyncio
async def test_request_human_input_normal_mode_still_pauses(tmp_path):
    from shared.tools.builtin import _request_human_input
    dc.set_bypass_mode(False)
    state = State.new(node_type="t", base_dir=tmp_path)
    res = await _request_human_input(state, question="要不要提交这个 SLURM 作业？",
                                     options=["提交", "不提交"],
                                     recommended_option_index=0)
    assert res["status"] == "pause"
    assert res["pause_event"]["question"] == "要不要提交这个 SLURM 作业？"


@pytest.mark.asyncio
async def test_request_human_input_bypass_mode_returns_success_no_pause(tmp_path):
    from shared.tools.builtin import _request_human_input
    dc.set_bypass_mode(True)
    state = State.new(node_type="t", base_dir=tmp_path)
    res = await _request_human_input(state, question="要不要提交这个 SLURM 作业？",
                                     options=["提交", "不提交"],
                                     recommended_option_index=0)
    assert res["status"] == "success"
    assert res["bypassed"] is True
    assert res["response"] == "提交"          # 默认选 options[0]
    assert res["asked_by"] == "t"
    assert "pause_event" not in res


@pytest.mark.asyncio
async def test_request_human_input_bypass_mode_no_options_gets_fallback_text(tmp_path):
    from shared.tools.builtin import _request_human_input
    dc.set_bypass_mode(True)
    state = State.new(node_type="t", base_dir=tmp_path)
    res = await _request_human_input(state, question="怎么处理这个 confound？")
    assert res["status"] == "success"
    assert res["bypassed"] is True
    assert "best judgment" in res["response"]


@pytest.mark.asyncio
async def test_request_human_input_bypass_writes_audit_event(tmp_path):
    import json
    from shared.tools.builtin import _request_human_input
    dc.set_bypass_mode(True)
    state = State.new(node_type="t", base_dir=tmp_path)
    await _request_human_input(state, question="q", options=["a", "b"],
                               recommended_option_index=0)
    lines = state.transcript_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(l) for l in lines if json.loads(l).get("event") == "human_input_bypassed"]
    assert len(events) == 1
    assert events[0]["n_options"] == 2
    assert events[0]["selected_option_index"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass", [False, True])
async def test_generic_human_input_proceeds_while_a_decision_package_is_pending(
    tmp_path, bypass,
):
    """判决拆除第三波（builtin:957 降格）：有正式 decision package 等人时，通用提问
    不再被拒 —— 提问本身不改 package 状态（它仍 awaiting_human，REVISE→PROCEED 的
    风险由 package 自身状态守住）。放行，但等人的清单如实附进事件与 transcript。
    墙加回去这条转红。"""
    import json
    from shared.tools.builtin import _request_human_input

    dc.set_bypass_mode(bypass)
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "writing",
        "producing_run_id": "r-writing",
        "review_state": "done",
        "curator_state": "done",
        "decision_state": "awaiting_human",
        "decision_recommended_action": "revise",
    }]

    res = await _request_human_input(
        state,
        question="请选择下一步",
        options=["PROCEED", "REVISE", "EDIT"],
        recommended_option_index=1,
    )

    assert res["status"] == ("success" if bypass else "pause"), res
    carried = res["pending_decisions"] if bypass else res["pause_event"]["pending_decisions"]
    assert carried[0]["recommended_action"] == "revise"
    # package 自身状态一字未动
    assert state.hook_state["pending_post_node_flow"][0]["decision_state"] == "awaiting_human"
    events = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    assert any(e.get("event") == "human_input_requested_while_decision_pending" for e in events)
    assert not any(e.get("event") == "generic_human_input_rejected" for e in events)
