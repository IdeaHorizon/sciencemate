"""「不要修改文件」从派发到终稿走一遍真路径（#973，wangd 2026-10-21 裁决）。

现场：用户说「只做检查和报告…不要修改文件」，会话实际新增 24 个 Project 文件，
终稿和审稿都说「未修改文件」。lujy 09-18 指出 #1009 那一层的三处断线：
  · 没人声明承诺、hook 没人启用；
  · 只认 `.research/`，节点证据被算成用户文件，真正的用户文件反而看不见；
  · 只读本节点 transcript，子节点写了什么调度器对不上。

这里走真入口：调度器 `run_node(read_only=True)` → executor 冻住子 run 的承诺 →
子 run 用真 write_file 写（真 `workspace_changed` 事件）→ `_finish_child` 汇总进
调度器的 transcript → 调度器自己的交付写权被拒 → 回复出口附上三类计数。
只把 LLM 循环换成一段固定的工具调用，判据落在盘上和 transcript 里。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.agent_loop import LoopResult
from core.bootstrap import bootstrap
from core.side_effect_contract import (
    append_statement,
    end_read_only,
    promised_read_only,
    reconcile,
)
from core.state import State
from core.tool_registry import execute


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text("schema_version: 2\nname: Test\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "model.py").write_text("print('user code')\n", encoding="utf-8")
    for directory in ("literature", "notes"):
        (root / directory).mkdir()
        (root / directory / "README.md").write_text(f"# {directory}\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "Initialize Project")
    return root


def _orchestrator(tmp_path: Path) -> tuple[State, Path]:
    bootstrap()
    project = _worktree(tmp_path)
    state = State.new("_orchestrator", tmp_path / "runs",
                      project_id="p973", project_worktree=project)
    state.hook_state["_callable_nodes"] = ["*"]
    state.hook_state["_deliverable_writes"] = True
    return state, project


def _events(state_or_root, name: str) -> list[dict]:
    path = (state_or_root.transcript_path if hasattr(state_or_root, "transcript_path")
            else Path(state_or_root) / "transcript.jsonl")
    if not path.exists():
        return []
    return [rec for rec in (json.loads(line) for line in
                            path.read_text(encoding="utf-8").splitlines() if line.strip())
            if rec.get("event") == name]


class _ChildWork:
    """替掉子 run 的 LLM 循环：做一次只读检查该做的事 —— 写自己的证据、试图改用户源码、
    在 run 目录留一份内部日志。"""

    def __init__(self):
        self.child: State | None = None
        self.refusal: dict | None = None

    async def __call__(self, harness, state, messages, llm):
        self.child = state
        ok = await execute("write_file", state, path="environment_audit.md",
                           content="# 环境检查\n只读检查，未编译未运行。\n")
        assert ok["status"] == "success", ok
        self.refusal = await execute("write_file", state, path="project/src/model.py",
                                     content="print('patched')\n")
        (Path(state.root) / "logs").mkdir(exist_ok=True)
        (Path(state.root) / "logs" / "0001.log").write_text("internal\n", encoding="utf-8")
        return LoopResult(final_text="检查完成。", turns=1, status="completed",
                          terminal_cause="model_finished")


def _dispatch(orch: State, *, read_only: bool, work: _ChildWork) -> dict:
    from shared.tools.run_node import _run_node_tool

    with patch("core.executor.run_loop", work):
        return asyncio.run(_run_node_tool(
            orch, "literature",
            node_inputs={"research_question": "检查这个项目能不能编译（只看不改）"},
            user_note="先做一次只读的环境检查。",
            read_only=read_only, read_only_note="只做检查和报告，不要修改文件",
        ))


# ── 1 + 5：子 run 继承承诺；用户源文件写不进去；节点证据照写 ──────────────────


def test_the_child_inherits_the_promise_and_cannot_touch_user_source(tmp_path):
    orch, project = _orchestrator(tmp_path)
    work = _ChildWork()

    out = _dispatch(orch, read_only=True, work=work)

    assert out["status"] == "success", out
    child = work.child
    assert promised_read_only(child) is not None, "子 run 没拿到派发方的只读声明"
    assert _events(child, "side_effect_contract_declared"), "承诺没进子 run 的永久记录"
    assert work.refusal["status"] == "error" and "只读约束" in work.refusal["error"], (
        f"只读约束下用户源文件照样写进去了：{work.refusal}")
    assert (project / "src/model.py").read_text() == "print('user code')\n"
    assert (project / "literature/environment_audit.md").exists(), "节点证据被误拦"


def test_the_promise_lives_on_disk_so_a_resumed_child_still_has_it(tmp_path):
    orch, _project = _orchestrator(tmp_path)
    work = _ChildWork()
    _dispatch(orch, read_only=True, work=work)

    reopened = State.reopen("literature", Path(work.child.root).parent, work.child.run_id)
    assert promised_read_only(reopened) is not None, "续跑之后只读约束丢了"


# ── 汇总：子 run 写了什么进调度器的 transcript ─────────────────────────────


def test_child_writes_roll_up_into_the_orchestrators_reconciliation(tmp_path):
    orch, _project = _orchestrator(tmp_path)
    out = _dispatch(orch, read_only=True, work=_ChildWork())

    rolled = _events(orch, "child_workspace_changed")
    assert rolled and "literature/environment_audit.md" in rolled[0]["paths"]
    r = reconcile(orch)
    assert r.promised_read_only and not r.contradiction
    assert "literature/environment_audit.md" in r.node_evidence
    assert r.user_source == []
    # 返回值当场把三类交到调度器手上
    assert out["side_effects"]["counts"]["node_evidence"] >= 1
    assert "不要说「未修改文件」" in out["side_effect_note"]


def test_internal_run_logs_never_count_as_project_changes(tmp_path):
    """验收 4：不进 Project Git 的运行日志（run 目录）不计；进 Git 的节点产物计为节点证据。"""
    orch, _project = _orchestrator(tmp_path)
    work = _ChildWork()
    _dispatch(orch, read_only=True, work=work)

    assert (Path(work.child.root) / "logs/0001.log").exists()
    every = [*reconcile(orch).user_source, *reconcile(orch).node_evidence,
             *reconcile(orch).bookkeeping]
    assert not any("0001.log" in p for p in every), every
    assert "literature/environment_audit.md" in every


def test_without_read_only_nothing_is_declared_but_writes_still_roll_up(tmp_path):
    """反作弊：没声明就没有承诺、没有附注 —— 但子 run 的改动照样汇总（那是记账，不是约束）。"""
    orch, project = _orchestrator(tmp_path)
    work = _ChildWork()
    out = _dispatch(orch, read_only=False, work=work)

    assert promised_read_only(orch) is None and promised_read_only(work.child) is None
    assert "side_effects" not in out
    assert _events(orch, "child_workspace_changed")
    # literature 本来就写不了项目根（作用域墙），报错不是只读约束那一句
    assert "只读约束" not in work.refusal.get("error", "")
    assert (project / "src/model.py").read_text() == "print('user code')\n"


def test_a_parallel_dispatch_carries_the_flag_the_same_way(tmp_path):
    """并行与串行同一语义（#1097 验收 1 的先例）：漏传一个参数，整条并行路径就没有约束。"""
    from shared.tools.run_node import _run_nodes_parallel

    orch, project = _orchestrator(tmp_path)
    work = _ChildWork()
    with patch("core.executor.run_loop", work):
        out = asyncio.run(_run_nodes_parallel(orch, jobs=[{
            "node_type": "literature", "user_note": "并行做一次只读检查。",
            "read_only": True, "read_only_note": "不要修改文件"}]))
    assert out["results"][0]["status"] == "success", out
    assert promised_read_only(work.child) is not None
    assert promised_read_only(orch) is not None
    assert "只读约束" in work.refusal["error"]


# ── 调度器自己：交付写权在只读约束下停用；notes/ 照写 ────────────────────────


@pytest.mark.asyncio
async def test_the_orchestrators_own_deliverable_write_is_refused_under_read_only(tmp_path):
    orch, project = _orchestrator(tmp_path)
    before = await execute("write_file", orch, path="project/BEFORE.md", content="x\n")
    assert before["status"] == "success", "前提：没有约束时交付写权照常"

    from core.side_effect_contract import declare_read_only

    declare_read_only(orch, quote="不要修改文件", declared_by_run_id=orch.run_id)
    refused = await execute("write_file", orch, path="project/REPORT.md", content="x\n")
    edited = await execute("edit_file", orch, path="project/src/model.py",
                           old_string="user code", new_string="patched")
    notes = await execute("write_file", orch, path="project/notes/check.md", content="ok\n")

    assert refused["status"] == "error" and "只读约束" in refused["error"]
    assert not (project / "REPORT.md").exists()
    assert edited["status"] == "error"
    assert (project / "src/model.py").read_text() == "print('user code')\n"
    assert notes["status"] == "success", "自己作用域里的记录是节点证据，不该被拦"
    r = reconcile(orch)
    assert r.node_evidence == ["notes/check.md"], "BEFORE.md 在承诺之前，不该算进来"
    assert r.user_source == []
    # 交付写权写的项目根文件不在调度器自己的 workspace_changed 里（它只观测 notes/），
    # 但作用域外见证记下了它 —— 改动清单看得见调度器写的用户源文件（lujy 09-18 第 2 条）。
    from core.side_effect_contract import changed_paths

    every_event = [json.loads(line) for line in
                   orch.transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert "BEFORE.md" in changed_paths(every_event)


@pytest.mark.asyncio
async def test_a_projection_onto_user_source_is_refused_under_read_only(tmp_path):
    """save_artifact 的交付投影是另一条写到项目根的路：账照落，投影被拒并吵出来。"""
    project = _worktree(tmp_path)
    bootstrap()
    child = State.new("literature", tmp_path / "runs", project_id="p973",
                      project_worktree=project)
    child.hook_state["_deliverable_projection"] = {
        "artifact_type": "survey_report", "path": "LITERATURE_REVIEW.md"}
    from core.side_effect_contract import declare_read_only

    declare_read_only(child)
    out = await execute("save_artifact", child, artifact_type="survey_report",
                        name="wmles", content="# 综述\n")
    assert out["status"] == "success", out
    assert "只读约束" in out.get("deliverable_projection_error", ""), out
    assert not (project / "LITERATURE_REVIEW.md").exists()


def test_a_dispatch_whose_deliverable_lands_on_user_source_is_refused_up_front(tmp_path):
    orch, project = _orchestrator(tmp_path)
    from shared.tools.run_node import _run_node_tool

    work = _ChildWork()
    with patch("core.executor.run_loop", work):
        out = asyncio.run(_run_node_tool(
            orch, "literature", user_note="写一份综述。", read_only=True,
            deliverable={"artifact_type": "survey_report", "path": "LITERATURE_REVIEW.md"},
        ))
    assert out["status"] == "error" and "只读约束" in out["error"], out
    assert work.child is None, "派发时就该说出来，而不是让子节点跑完才发现"


# ── 2：终稿按三类说；零就说零；承诺属于一次请求 ─────────────────────────────


def test_the_reply_states_three_classes_and_never_claims_zero(tmp_path):
    orch, _project = _orchestrator(tmp_path)
    _dispatch(orch, read_only=True, work=_ChildWork())

    reply = append_statement(orch, "全程只读，未修改文件。")

    tail = reply.split("\n\n")[-1]
    assert "你的源文件 0 个" in tail and "节点证据 1 个" in tail and ".research 记账" in tail
    assert "没有新增或修改任何" not in tail
    assert append_statement(orch, reply) == reply, "同一份事实附了两遍"


def test_a_new_user_message_ends_the_promise_but_a_continuous_turn_does_not(tmp_path, monkeypatch):
    import chat as chat_mod
    import core.pause as pause_mod
    from core.session_driver import SessionFrontend, run_turn
    from core.side_effect_contract import declare_read_only

    orch, _project = _orchestrator(tmp_path)

    async def _raw(state, harness, messages, llm, user_text):
        return LoopResult(final_text="好的。", turns=1, status="completed",
                          terminal_cause="model_finished")

    async def _post(result, harness, state, messages, llm, *, ask_pause=None):
        return result.final_text

    monkeypatch.setattr(chat_mod, "_run_one_turn_raw", _raw)
    monkeypatch.setattr(chat_mod, "_post_loop_reply", _post)
    monkeypatch.setattr(pause_mod, "get_deepest_paused", lambda: None)

    class _Front(SessionFrontend):
        ask_pause = None
        waits_for_background = False

        def emit(self, event, **fields):
            pass

    declare_read_only(orch, quote="不要修改文件")
    kept = asyncio.run(run_turn(orch, None, [], None,
                                chat_mod._CONTINUOUS_INTERNAL_PREFIX + " 继续",
                                frontend=_Front()))
    assert promised_read_only(orch) is not None, "框架自动续轮不是新请求"
    assert "文件改动核对" in kept.reply and "没有新增或修改任何 Project 文件" in kept.reply

    fresh = asyncio.run(run_turn(orch, None, [], None, "谢谢，帮我把结论写成文档",
                                 frontend=_Front()))
    assert promised_read_only(orch) is None, "新请求还挂着上一请求的只读约束"
    assert "文件改动核对" not in fresh.reply
    assert _events(orch, "side_effect_contract_ended")
    assert end_read_only(orch, reason="again") is False
