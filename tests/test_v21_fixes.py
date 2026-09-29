"""Reminder hook smoke tests（v3 cleanup 后保留版本）。

只保留与 v3 schema 仍兼容的部分：
  - producing → curator pending integration 注册
  - producing_integration_reminder hook
  - dreaming_due_reminder hook（基于 curator_run audit）
  - orchestrator harness 加载 reminder hooks
  - tool_registry.execute 不与 name kwarg 撞名（regression）

v2 时代针对 6-entity 强注入的测试已废弃；KB 字段渲染验证移到
test_kb_schema.py。
"""
from __future__ import annotations

import pytest

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import bootstrap

bootstrap()

from core.state import State                                    # noqa: E402
from core.tool_registry import execute as ex, get_tool          # noqa: E402


def make_state(td: Path, node_type: str = "test",
               project_id: str | None = None) -> State:
    os.environ["HARNESS_FRAMEWORK_HOME"] = str(td)
    return State.new(node_type=node_type, base_dir=td, project_id=project_id)


# ─────────────────────────────────────────────────────────────────────────────
# producing → curator pending integration
# ─────────────────────────────────────────────────────────────────────────────

# 2026-08-19：`_producing_integration_reminder_on_turn_start` 连同它提醒的那份
# legacy 镜像 `pending_curator_integrations` 一起删除 —— curator 退出 post-producing
# flow，成为按需调取的后台节点，不再需要每轮催调度器"先去整合"。
# 新形态见 tests/test_curator_is_out_of_the_flow.py。
#
# 2026-09-17：`test_pending_integration_registered_on_producing_complete` 也一并删除。
# 它断言的两件事今天都不成立、也都没有被测对象：
#   · `"literature" in PRODUCING_NODE_TYPES` —— 那张回退名单已删（literature 早已是
#     服务节点，名单里还留着 analysis 这个被删掉的节点，见 core/loader）；
#   · 本体是往一个普通 dict 里 append 一条再断言 `len(...) == 1` —— 测的是
#     `list.append`，而它操作的 `pending_curator_integrations` 正是上面这条注释说
#     已经删掉的那份 legacy 镜像。**测一个已删机制的替身，绿色不指向任何东西。**

def test_orchestrator_has_reminder_hooks_enabled():
    from core.loader import load_harness
    # v0.4: post_node_review_flow_reminder 取代 producing_integration_reminder
    h = load_harness("_orchestrator")
    assert "post_node_review_flow_reminder" in h.loop_hooks
    assert "dreaming_due_reminder" in h.loop_hooks
    print("  ✓ _orchestrator harness 启用 post_node_review_flow + dreaming_due reminder hooks")
    return


def _test_old_hook_still_works_disabled():
    from core.loader import load_harness
    h = load_harness("_orchestrator")
    assert "producing_integration_reminder" in h.loop_hooks
    assert "dreaming_due_reminder" in h.loop_hooks
    print("  ✓ _orchestrator harness 启用两个 reminder hook")


# ─────────────────────────────────────────────────────────────────────────────
# dreaming_due reminder
# ─────────────────────────────────────────────────────────────────────────────

def test_dreaming_due_when_no_history():
    from core.loop_hooks_builtin import _check_dreaming_due
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        is_due, last_at = _check_dreaming_due(state, max_age_days=7)
        assert is_due is True
        assert last_at is None
    print("  ✓ 无历史 → dreaming_due=True")


def _seed_curator_run(home: Path, pid: str, when) -> str:
    """在 run 账本里种一次跑完的 curator run。

    判据从 `curator_audit`（全仓零写入方，永远判"从未跑过"）改成 run 账本 ——
    那个 hook 曾经因此对每个派发过子节点的 session 永久唠叨。
    """
    import json as _json

    stamp = when.isoformat()
    run_id = f"{int(when.timestamp())}-cccccc"
    d = home / "projects" / pid / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(_json.dumps({
        "run_id": run_id, "node_type": "_curator", "status": "completed",
        "ended_at": stamp,
    }), encoding="utf-8")
    return stamp


def test_dreaming_due_when_recent():
    from core.loop_hooks_builtin import _check_dreaming_due
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td), project_id="p_recent")
        stamp = _seed_curator_run(
            Path(td), "p_recent", datetime.now(timezone.utc) - timedelta(days=1))
        is_due, last_at = _check_dreaming_due(state, max_age_days=7)
        assert is_due is False
        assert last_at == stamp
    print("  ✓ 1 天前跑过 dreaming → due=False")


def test_dreaming_due_when_stale():
    from core.loop_hooks_builtin import _check_dreaming_due
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td), project_id="p_stale")
        stamp = _seed_curator_run(
            Path(td), "p_stale", datetime.now(timezone.utc) - timedelta(days=10))
        is_due, last_at = _check_dreaming_due(state, max_age_days=7)
        assert is_due is True
        assert last_at == stamp
    print("  ✓ 10 天前跑过 → due=True")


def test_dreaming_reminder_hook_injects_when_due():
    from core.loop_hooks_builtin import _dreaming_due_on_turn_start
    from core.loop_hooks import HookContext
    from core.harness import NodeHarness

    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        ctx = HookContext(
            harness=NodeHarness(node_type="_orchestrator"),
            state=state, messages=[], turn=1,
        )
        # v2.1 相关性门控：这是项目级维护提醒，只在本 session 真的在做项目
        # 工作（起过子节点）时才打扰用户。纯问答/闲聊不该被推销 KB 维护。
        assert _dreaming_due_on_turn_start(ctx) is None, "没做项目工作时不得提醒"
        ctx.state.hook_state["_dispatched_any_child"] = True

        result = _dreaming_due_on_turn_start(ctx)
        assert result is not None and len(result) == 1
        assert "dreaming_due" in result[0].content
        assert "Mode 2" in result[0].content
    print("  ✓ dreaming_due hook 在 due 时注入 system message")


# ─────────────────────────────────────────────────────────────────────────────
# curator_revert 工具描述含 lifecycle 不可撤的警告
# ─────────────────────────────────────────────────────────────────────────────

def test_curator_revert_tool_description_has_warning():
    t = get_tool("curator_audit")
    assert t is not None
    desc = t.description.lower()
    assert "lifecycle" in desc or "撤不了" in desc or "review_history" in desc
    print("  ✓ curator_revert 工具描述含 lifecycle 不可撤的警告")


# ─────────────────────────────────────────────────────────────────────────────
# Regression: tool_registry.execute 不与 LLM 传的 name kwarg 撞名
# ─────────────────────────────────────────────────────────────────────────────

def test_execute_does_not_collide_with_name_kwarg():
    """LLM 调 save_artifact(name='foo')，execute 不应抛 TypeError。"""
    async def _go():
        st = State.new(node_type="t", base_dir=Path(tempfile.mkdtemp()))
        out = await ex("save_artifact", st,
                          artifact_type="test", name="foo", content="bar")
        assert isinstance(out, dict)
        assert "got multiple values" not in str(out)
    asyncio.run(_go())
    print("  ✓ execute 不与 name kwarg 撞名")


if __name__ == "__main__":
    print("== reminder hook smoke tests ==\n")
    tests = [
        test_pending_integration_registered_on_producing_complete,
        test_producing_integration_reminder_hook_injects_when_pending,
        test_orchestrator_has_reminder_hooks_enabled,
        test_dreaming_due_when_no_history,
        test_dreaming_due_when_recent,
        test_dreaming_due_when_stale,
        test_dreaming_reminder_hook_injects_when_due,
        test_curator_revert_tool_description_has_warning,
        test_execute_does_not_collide_with_name_kwarg,
    ]
    for t in tests:
        print(f"- {t.__name__}")
        t()
    print(f"\nALL {len(tests)} TESTS PASS ✓")


@pytest.mark.asyncio
async def test_finish_gate_blocks_completion_when_mandatory_selfcheck_never_ran(tmp_path):
    """收尾闸：节点声明的强制自检没跑过就不许收尾 —— prompt 里的'必须'不是机制。

    E2E 实测（2026-08-07 真课题 LJ 冷却）：hypothesis 的 prompt 里"结束前必须
    validate_hypothesis_outputs() passed=true"写了三遍（工作流第 11 步 /
    rules / QC 描述），agent 全程 56 次工具调用**一次没调**，最后一轮还在
    create_claim 就收工。on_end hook 补跑校验时 loop 已结束 —— 失败的两项
    （research_plan_complete 的 DAG 说明、hypothesis_research_overview）都是
    它当场能补的，却只能判 incomplete，还拖累下游 reviewer 白跑 40 轮。
    """
    from core import loop_hooks
    from core.loop_hooks import HookContext, LoopHook
    from core.llm import LLMMessage
    from core.state import State

    calls = {"n": 0}

    async def _gate(ctx):
        calls["n"] += 1
        if ctx.state.hook_state.get("selfcheck_done"):
            return None                      # 自检做过 → 放行
        return [LLMMessage(role="system", content="⛔ 收尾被拦下：先跑自检")]

    hooks = [LoopHook(name="t_gate", on_before_finish=_gate)]
    state = State.new(node_type="hypothesis", base_dir=tmp_path, project_id="p_gate")
    ctx = HookContext(state=state, harness=None, turn=1, messages=[])

    blocked = await loop_hooks.run_on_before_finish(hooks, ctx)
    assert blocked and "收尾被拦下" in blocked[0].content

    state.hook_state["selfcheck_done"] = True
    assert await loop_hooks.run_on_before_finish(hooks, ctx) == []
    assert calls["n"] == 2

    # 没声明 on_before_finish 的 hook 不受影响
    assert await loop_hooks.run_on_before_finish([LoopHook(name="plain")], ctx) == []


def test_file_scoped_node_has_no_git_artifacts_dir(tmp_path):
    """作用域是一个文件的节点不得拼出 `<file>/artifacts`。

    curator 的写作用域是 MEMORY.md（一个文件）。此前产物目录直接把
    workspace_relative_path 当目录拼 `/artifacts`，于是 E2E v7 里 curator
    连挂三次 NotADirectoryError，整条 orchestrator 流被拖死。
    写作用域 ≠ 记录目录：形状由 bind_project_workspace 单点判定 —— 文件作用域
    的节点没有 Git 内记录目录（`workspace_records_dir is None`），记录落 run 本地；
    目录作用域的节点记录就落它自己的节点目录（正文即文件，没有 artifacts/ 这层）。
    """
    import subprocess

    from core.project_workspace import bind_project_workspace
    from core.state import State

    root = tmp_path / "wt"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=root,
                   check=True, env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                    "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                    "GIT_COMMITTER_EMAIL": "t@t"})

    curator = State(run_id="r1", node_type="_curator", root=tmp_path / "run-cur")
    bind_project_workspace(curator, root)
    assert curator.workspace_records_dir is None
    assert curator.records_dir == (tmp_path / "run-cur") / "artifacts"
    assert (root / "MEMORY.md").is_file()

    hypo = State(run_id="r2", node_type="hypothesis", root=tmp_path / "run-hyp")
    bind_project_workspace(hypo, root)
    assert hypo.workspace_records_dir == root / "plan"
    assert hypo.records_dir == root / "plan"
    assert hypo.records_dir.is_dir()


def test_service_success_does_not_break_a_producing_failure_chain(tmp_path):
    """P5 实测：postprocess 连挂 6 次同两个 QC，中间夹一次 data 服务成功，
    连续失败链就被判断开 → 熔断哑火 → orchestrator 一路重派烧 3M token。

    `break_on_other_producing_success` 的语义是"上游状况变了"。**服务**跑成功
    不代表任何状况改变 —— 它是被随手调用的。判据取自节点自己的 post_run_flow
    声明，不是"名字不以下划线开头"。
    """
    from core import run_history

    def _rec(node_type, completed, signals=()):
        return run_history.RunRecord(
            run_id=f"r-{node_type}-{completed}-{len(signals)}",
            node_type=node_type,
            project_id="p",
            status="completed" if completed else "incomplete",
            missing_required_outputs=list(signals),
            state_dir=tmp_path,
        )

    sig = ["figure_binding_valid"]
    runs = [
        _rec("postprocess", False, sig),
        _rec("data", True),                 # 服务成功 —— 不该断链
        _rec("postprocess", False, sig),
        _rec("postprocess", False, sig),
    ]
    found = run_history.consecutive_failures(
        runs, "postprocess", break_on_other_producing_success=True)
    assert found is not None and found["count"] == 3

    # producing 节点成功仍然断链（原语义不动）
    runs_with_producer = [
        _rec("postprocess", False, sig),
        _rec("hypothesis", True),
        _rec("postprocess", False, sig),
        _rec("postprocess", False, sig),
    ]
    assert run_history.consecutive_failures(
        runs_with_producer, "postprocess",
        break_on_other_producing_success=True) is None


def test_repeat_dispatch_breaker_is_active_under_project_v2():
    """曾因'v2 会交回结构化 blocker'把 v2 整个排除 → v2 下完全没有熔断。

    判据本来就是按**信号**算的（同一失败信号连续出现），不是盲目重试计数，
    所以那个排除理由不成立。这和 reviewer 门当初排除 v2 是同一个错误。
    """
    import inspect

    from shared.tools import run_node

    source = inspect.getsource(run_node)
    assert "_repeat = _repeated_failure_for(state, node_type)" in source
    assert 'if getattr(state, "project_worktree", None) is not None\n        else _repeated_failure_for' not in source
