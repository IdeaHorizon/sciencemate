"""Project 模式 forward_artifact_ids = 输入选择（不搬文件，选择必须送达）。

## 根因（2026-08-18）

run_node 在 Project 模式整个忽略 forward_artifact_ids（发
artifact_forward_ignored_project_v2 事件、工具描述让 caller "不用费心挑"）。
同类型多份可见时子节点没有任何机械通道知道用哪份，writing 只好自造
`upstream_artifact_inventory` 私有申报格式绕行 —— 其宽容解析器把写错形状的
选择静默丢弃，调度器整晚困在假 ambiguous 里。

## 修复后的链路（本文件三段各测一环）

  run_node 验 id → execute_node(selected_input_ids=...) →
  executor 落 hook_state["forwarded_input_ids"] + transcript →
  writing input_audit 机械读取（见 test_writing_harness_contract）。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from core.bootstrap import bootstrap
from core.llm import LLMResponse
from core.state import State

bootstrap(force=True)


def _project_worktree(tmp_path: Path) -> Path:
    root = tmp_path / "selection-project"
    root.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Selection Test"],
        ["config", "user.email", "selection@example.test"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "project.yaml").write_text(
        "schema_version: 2\nname: Forward selection test\n", encoding="utf-8"
    )
    for node in ["hypothesis", "literature", "writing"]:
        (root / node / "artifacts").mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "init"], check=True, capture_output=True
    )
    return root


def _transcript_events(state_dir: Path) -> list[dict]:
    path = state_dir / "transcript.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _fake_child_summary(kw: dict) -> dict:
    return {
        "run_id": "fake_child_run",
        "node_type": kw["node_type"],
        "project_id": kw.get("project_id"),
        "status": "completed",
        "missing_required_outputs": [],
        "turns": 1,
        "tool_call_count": 0,
        "artifacts": [],
        "final_text_preview": "",
        "state_dir": str(Path(tempfile.mkdtemp())),
        "project_root": None,
        "depth": 1,
        "sub_run_id": "test",
    }


# ── 1. run_node：project 模式下 forward = 验过 id 的选择，随派发下传 ────────

def test_project_forward_ids_delivered_as_selection(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from core.tool_registry import execute

    worktree = _project_worktree(tmp_path)
    state = State.new(
        node_type="hypothesis",
        base_dir=tmp_path / "runs",
        project_id="selection-test",
        project_worktree=worktree,
    )
    state.depth = 0
    state.hook_state["_callable_nodes"] = ["literature"]
    aid = state.save_artifact("survey_report", "explicit_one", "survey body")["id"]

    captured: dict = {}

    async def fake_execute_node(**kw):
        captured.update(kw)
        return _fake_child_summary(kw)

    import core.executor

    monkeypatch.setattr(core.executor, "execute_node", fake_execute_node)
    monkeypatch.setattr("core.llm.LLMClient", lambda: MagicMock())

    result = asyncio.run(
        execute(
            "run_node", state,
            node_type="literature",
            user_note="测试派发",
            node_inputs={"research_question": "x"},
            forward_artifact_ids=[aid],
        )
    )

    assert result["status"] == "success", result
    # 不搬文件……
    assert captured["upstream_artifacts"] == []
    # ……但选择送达
    assert captured["selected_input_ids"] == [aid]
    assert result["framework_autoresolved_inputs"]["selected_input_ids"] == [aid]
    events = _transcript_events(state.root)
    selection = [e for e in events if e["event"] == "artifact_selection_forwarded_project"]
    assert selection and selection[0]["artifact_ids"] == [aid]
    # 旧的"忽略"事件不复存在
    assert not any(e["event"] == "artifact_forward_ignored_project_v2" for e in events)


def test_project_forward_missing_id_fails_loud(monkeypatch, tmp_path):
    """id 不存在必须在派发处报错 —— 而不是让子节点拿着幽灵选择跑完才发现。"""
    from unittest.mock import MagicMock

    from core.tool_registry import execute

    worktree = _project_worktree(tmp_path)
    state = State.new(
        node_type="hypothesis",
        base_dir=tmp_path / "runs",
        project_id="selection-test",
        project_worktree=worktree,
    )
    state.depth = 0
    state.hook_state["_callable_nodes"] = ["literature"]

    async def fake_execute_node(**kw):  # pragma: no cover - 不应被调到
        raise AssertionError("dispatch should fail before execute_node")

    import core.executor

    monkeypatch.setattr(core.executor, "execute_node", fake_execute_node)
    monkeypatch.setattr("core.llm.LLMClient", lambda: MagicMock())

    result = asyncio.run(
        execute(
            "run_node", state,
            node_type="literature",
            user_note="测试派发",
            node_inputs={"research_question": "x"},
            forward_artifact_ids=["survey_report__nope"],
        )
    )
    assert result["status"] == "error"
    assert "survey_report__nope" in result["error"]
    assert "list_artifacts" in result["error"]


# ── 2. executor：选择落 hook_state + transcript（子 run 侧的机械真相源）────

def test_executor_records_selection_in_project_mode(tmp_path):
    from core.executor import execute_node
    from core.harness import NodeHarness

    worktree = _project_worktree(tmp_path)
    parent = State.new(
        node_type="hypothesis",
        base_dir=tmp_path / "parent-runs",
        project_id="selection-test",
        project_worktree=worktree,
    )
    aid = parent.save_artifact("survey_report", "explicit_one", "survey body")["id"]

    class _FinalTextLLM:
        model = "mock"

        async def chat(self, *args, **kwargs):
            return LLMResponse(
                content="done",
                tool_calls=[],
                finish_reason="stop",
                usage={"total_tokens": 5},
            )

    harness = NodeHarness(
        node_type="test_node",
        version="0.1",
        system_prompt="test",
        rules=[],
        guidelines=[],
        skills=[],
        expected_outputs={},
        tools=[],
        max_turns=2,
        kb_query="_disable",
    )

    summary = asyncio.run(
        execute_node(
            "test_node",
            state_dir=tmp_path / "child-runs",
            project_id="selection-test",
            harness_override=harness,
            llm=_FinalTextLLM(),
            parent_state=parent,
            depth=1,
            sub_run_id="sel-test",
            selected_input_ids=[aid],
        )
    )

    events = _transcript_events(Path(summary["state_dir"]))
    recorded = [e for e in events if e["event"] == "forwarded_inputs_recorded"]
    assert recorded and recorded[0]["artifact_ids"] == [aid]
