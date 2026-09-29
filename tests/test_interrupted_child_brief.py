"""被打断子 run 的既成事实必须机械送达恢复后的调度器。

现场（2026-08-18，会话 c9deb4f2）：curator 跑到第 35 个动作时 worker 被杀
（后端重启连带子进程）。恢复后调度器重新派了一个 curator 从头跑，Q1 命题在
KB 里被注册了两次 —— 中断的代价不只是浪费，是**不幂等副作用做两遍的腐蚀**。

事实全在盘上（transcript + workspace_changed），只差送达。同 PR#398
（截断≠做完）：修法是机械送达，不是指望模型猜。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.state import State
from shared.tools.run_node import brief_interrupted_child_runs


def _write_transcript(run_dir: Path, records: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "transcript.jsonl").open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _orchestrator(tmp_path: Path) -> State:
    return State.new(node_type="_orchestrator", base_dir=tmp_path / "orch")


def _child_records(parent_run_id: str, *, node_type: str = "_curator",
                    n_tools: int = 3, ended: bool = False) -> list[dict]:
    records: list[dict] = [
        {"event": "run_start", "node_type": node_type,
         "parent_run_id": parent_run_id, "sub_run_id": f"x->{node_type}@d1"},
    ]
    for i in range(n_tools):
        records.append({"event": "tool_call", "turn": i + 1, "name": f"tool_{i}"})
    records.append({
        "event": "workspace_changed", "tool_name": "register_claim",
        "paths": ["knowledge/claims/q1_attribution.md"],
    })
    records.append({"event": "llm_response",
                    "content": "Q1 归因命题已注册，继续登记证据链。"})
    if ended:
        records.append({"event": "run_end", "status": "completed"})
    return records


def test_interrupted_child_facts_reach_the_orchestrator(tmp_path: Path) -> None:
    state = _orchestrator(tmp_path)
    _write_transcript(
        state.root.parent / "run_curator_dead",
        _child_records(state.run_id, n_tools=35),
    )
    briefed = brief_interrupted_child_runs(state)
    assert briefed == 1
    injected = state.hook_state["injected_messages"]
    assert len(injected) == 1
    content = injected[0]["content"]
    assert "35 次工具调用" in content
    assert "knowledge/claims/q1_attribution.md" in content
    assert "Q1 归因命题已注册" in content
    assert injected[0]["source"] == "system_recovery"


def test_completed_children_and_strangers_are_not_briefed(tmp_path: Path) -> None:
    state = _orchestrator(tmp_path)
    # 正常完成的子 run：有 run_end，不该出现在简报里。
    _write_transcript(
        state.root.parent / "run_done",
        _child_records(state.run_id, ended=True),
    )
    # 别的调度器的后代：血缘爬不到我，不该出现。
    _write_transcript(
        state.root.parent / "run_foreign",
        _child_records("orchestrator__someone_else"),
    )
    assert brief_interrupted_child_runs(state) == 0
    assert "injected_messages" not in state.hook_state


def test_grandchildren_are_covered_via_lineage(tmp_path: Path) -> None:
    """literature 由 hypothesis 派出（孙代）—— 血缘爬两级也要覆盖。"""
    state = _orchestrator(tmp_path)
    _write_transcript(
        state.root.parent / "run_hypo",
        _child_records(state.run_id, node_type="hypothesis", ended=True),
    )
    _write_transcript(
        state.root.parent / "run_lit",
        _child_records("run_hypo", node_type="literature", n_tools=8),
    )
    assert brief_interrupted_child_runs(state) == 1
    assert "literature" in state.hook_state["injected_messages"][0]["content"]


def test_briefing_happens_once_per_corpse(tmp_path: Path) -> None:
    """第二次恢复不重复唠叨 —— hook_state 记账。"""
    state = _orchestrator(tmp_path)
    _write_transcript(
        state.root.parent / "run_curator_dead",
        _child_records(state.run_id),
    )
    assert brief_interrupted_child_runs(state) == 1
    assert brief_interrupted_child_runs(state) == 0
    assert len(state.hook_state["injected_messages"]) == 1


def test_a_corrupt_corpse_does_not_kill_recovery(tmp_path: Path) -> None:
    """单个尸体损坏（半行 JSON / 空文件）→ 跳过，恢复流程不死。"""
    state = _orchestrator(tmp_path)
    bad = state.root.parent / "run_bad"
    bad.mkdir(parents=True)
    (bad / "transcript.jsonl").write_text("{half json\n\n", encoding="utf-8")
    _write_transcript(
        state.root.parent / "run_ok",
        _child_records(state.run_id),
    )
    assert brief_interrupted_child_runs(state) == 1


# ── 续跑：同一个 run 接着走，不新开（wangd 2026-08-18 的核心要求）──────────

def _checkpoint(run_dir: Path, *, turn: int, texts: list[str]) -> None:
    (run_dir / "messages_checkpoint.json").write_text(json.dumps({
        "turn": turn,
        "written_at": "2026-08-18T00:00:00Z",
        "messages": [{"role": "system", "content": "旧的 system prompt"}]
        + [{"role": "assistant", "content": t} for t in texts],
    }, ensure_ascii=False), encoding="utf-8")


def test_a_dispatch_resumes_the_interrupted_run_of_the_same_node(tmp_path: Path) -> None:
    """派发同类节点 → 框架自动接上被打断那条，而不是新开一个。"""
    from shared.tools.run_node import _resumable_run_for

    state = _orchestrator(tmp_path)
    dead = state.root.parent / "run_curator_dead"
    _write_transcript(dead, _child_records(state.run_id, node_type="_curator"))
    _checkpoint(dead, turn=12, texts=["已注册 Q1 命题"])

    found = _resumable_run_for(state, "_curator")
    assert found is not None and found["run_id"] == "run_curator_dead"
    # 别的节点类型不该被误接
    assert _resumable_run_for(state, "literature") is None


def test_a_run_without_a_checkpoint_is_not_resumable(tmp_path: Path) -> None:
    """没有 checkpoint 就没有"之前那堆 message"可喂 —— 只能新开，如实。"""
    from shared.tools.run_node import _resumable_run_for

    state = _orchestrator(tmp_path)
    _write_transcript(
        state.root.parent / "run_no_ckpt",
        _child_records(state.run_id, node_type="_curator"),
    )
    assert _resumable_run_for(state, "_curator") is None


def test_a_completed_run_is_never_resumed(tmp_path: Path) -> None:
    from shared.tools.run_node import _resumable_run_for

    state = _orchestrator(tmp_path)
    done = state.root.parent / "run_done"
    _write_transcript(done, _child_records(state.run_id, node_type="_curator", ended=True))
    _checkpoint(done, turn=9, texts=["完成"])
    assert _resumable_run_for(state, "_curator") is None


def test_reopen_keeps_the_same_run_id_and_appends_to_the_same_transcript(
    tmp_path: Path,
) -> None:
    """续跑不是新开：同 run_id、同目录、transcript 继续追加。"""
    from core.state import State

    first = State.new(node_type="_curator", base_dir=tmp_path / "runs")
    first.append_transcript("run_start", node_type="_curator")
    again = State.reopen(
        node_type="_curator", base_dir=tmp_path / "runs", run_id=first.run_id
    )
    assert again.run_id == first.run_id
    assert again.root == first.root
    again.append_transcript("run_resumed_after_interruption", resumed_from_turn=12)
    events = [
        json.loads(line)["event"]
        for line in first.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events == ["run_start", "run_resumed_after_interruption"]


def test_executor_restores_checkpoint_messages_on_resume(tmp_path: Path) -> None:
    """executor 的 resume 分支：system 用当前的，历史接 checkpoint，并追加续跑指令。"""
    import asyncio
    from unittest.mock import patch

    from core.bootstrap import bootstrap
    from core.state import State

    bootstrap()
    runs = tmp_path / "runs"
    seed = State.new(node_type="_curator", base_dir=runs)
    seed.append_transcript("run_start", node_type="_curator")
    _checkpoint(seed.root, turn=7, texts=["我已经把 Q1 命题注册进 KB 了"])

    captured: dict = {}

    async def _fake_run_loop(harness, state, messages, llm):
        captured["messages"] = messages
        captured["run_id"] = state.run_id
        raise RuntimeError("stop here — 只验证入口装配")

    with patch("core.executor.run_loop", _fake_run_loop):
        try:
            asyncio.run(_execute(runs, seed.run_id))
        except Exception:
            pass

    assert captured.get("run_id") == seed.run_id, "续跑必须是同一个 run"
    texts = [m.content or "" for m in captured["messages"]]
    assert any("我已经把 Q1 命题注册进 KB 了" in t for t in texts), "没接上 checkpoint 历史"
    assert any("接着往下做" in t for t in texts), "没给出续跑指令"
    assert any("不幂等" in t for t in texts), "没警示重复副作用"


async def _execute(runs: Path, run_id: str):
    from core.executor import execute_node

    return await execute_node(
        "_curator", state_dir=runs, project_id=None, resume_run_id=run_id,
    )


def test_resumable_is_scoped_to_the_session_not_to_who_dispatched_it(tmp_path: Path) -> None:
    """上次由 orchestrator 派、这次由 hypothesis 派 —— 照样接上那条。

    2026-08-18 实测：第一版按血缘过滤，从 hypothesis 的视角那条 literature
    尸体不是它的后代，于是又新开了一条。而"上次那个还没跑完"跟"这次是谁派的"
    没有关系：它是**这个会话**里未完成的工作。
    """
    from shared.tools.run_node import _resumable_run_for

    runs = tmp_path / "runs"
    orch = State.new(node_type="_orchestrator", base_dir=runs, session_id="sess-A")
    # orchestrator 派的 literature，跑一半被杀
    dead = runs / "run_lit_dead"
    _write_transcript(dead, [
        {"event": "run_start", "node_type": "literature",
         "parent_run_id": orch.run_id, "session_id": "sess-A"},
        {"event": "tool_call", "name": "search_papers"},
    ])
    _checkpoint(dead, turn=5, texts=["检索到 17 篇"])
    # 现在是 hypothesis 在派 literature —— 它不是那条尸体的父节点
    hypo = State.new(node_type="hypothesis", base_dir=runs, session_id="sess-A")
    found = _resumable_run_for(hypo, "literature")
    assert found is not None and found["run_id"] == "run_lit_dead"


def test_another_sessions_corpse_is_never_resumed(tmp_path: Path) -> None:
    """边界仍是边界：别的会话的未完成 run 不许被这个会话接走。"""
    from shared.tools.run_node import _resumable_run_for

    runs = tmp_path / "runs"
    mine = State.new(node_type="hypothesis", base_dir=runs, session_id="sess-A")
    other = runs / "run_other_session"
    _write_transcript(other, [
        {"event": "run_start", "node_type": "literature",
         "parent_run_id": "someone-else", "session_id": "sess-B"},
        {"event": "tool_call", "name": "search_papers"},
    ])
    _checkpoint(other, turn=3, texts=["别的会话的活"])
    assert _resumable_run_for(mine, "literature") is None


def test_both_resume_entry_points_actually_call_the_brief() -> None:
    """接线：**平台实际走的** serve 路径必须也调简报。

    2026-08-18 实测教训：第一版只接在 platform_runtime 的一次性执行路径上，
    而平台走的是 `--serve` 常驻路径 —— 简报在平台上一次都没触发过，而所有
    单测都绿。「机制存在但没接到路径」的同款，这次是我自己现造的。

    两条恢复路径（serve / CLI 续连）各自都得调；判据锚在"恢复历史对话"
    那个动作旁边（recover_interrupted_decision_actions），它们是一对。
    """
    import ast
    import inspect

    import platform_runtime

    src = inspect.getsource(platform_runtime)
    tree = ast.parse(src)
    recovered = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "recover_interrupted_decision_actions"
    ]
    briefed = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "brief_interrupted_child_runs"
    ]
    assert len(recovered) >= 2, "恢复路径少了 —— 这条测试的锚点变了，先确认拓扑"
    assert len(briefed) == len(recovered), (
        f"有 {len(recovered)} 条恢复路径，但只有 {len(briefed)} 条调了简报 —— "
        "漏掉的那条上，被打断的子 run 对调度器不可见"
    )

    import chat

    chat_src = inspect.getsource(chat)
    assert "brief_interrupted_child_runs(state)" in chat_src, "CLI 续连路径没接简报"
