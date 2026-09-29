"""v3.2 的两道 orchestrator 墙在判决拆除·第三波之后的形态。

v9 实测事故：orchestrator 自己 save_artifact(type='manuscript') 写了整篇论文，
writing 节点全程没被调起；project_synthesis verdict='iterate' 也被无视。当年补了
两道墙：
  1. save_artifact：架构节点不能新建 producing 节点专属 deliverable type
  2. run_node(writing) 硬卡：必须有 verdict='ready_to_write' 的 project_synthesis

两道都是资格/流程判决（S3），删了（builtin:312 / run_node:1690/1725 降格）。
账不假靠的是**记录**：
  · produced_by_node_type 由框架盖章 —— 一份 `_orchestrator` 名下的 manuscript 就是
    代笔的 manuscript，不过 writing 的任何 QC 门，referee 终审看得见；
  · writing 的 input_audit 把「从未做过综合评估 / verdict 仍是 iterate」写进
    scientific_basis，稿件局限节须披露。

本文件每条「从前被拒、现在放行且如实入账」的用例，墙加回去就转红。
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.artifact_provenance import forwarded, produced
from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute, get_tool


@pytest.fixture(autouse=True)
def _bootstrap():
    bootstrap()


def _review_provenance(state: State) -> dict:
    return forwarded(
        produced("_reviewer", "review-run"),
        via_node_type=state.node_type,
        via_run_id=state.run_id,
    )


# ── 1：save_artifact 不再按角色拒新建，改盖章 ─────────────────────────────

def test_orchestrator_creating_a_manuscript_is_stamped_not_refused(tmp_path):
    """从前 error；现在 success，且记录顶层的章是 `_orchestrator`（框架盖，模型写不了）。"""
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p1")
    res = asyncio.run(execute(
        "save_artifact", state,
        artifact_type="manuscript", name="my_paper", content="# fake paper",
    ))
    assert res.get("status") == "success", res
    rec = state.read_artifact(res["id"])
    assert rec["produced_by_node_type"] == "_orchestrator"
    assert rec["produced_by_node_type"] != "writing"


def test_orchestrator_can_overwrite_existing_manuscript(tmp_path):
    """"小改"路径照旧放行：writing 节点先产出，orchestrator 改已存在的。"""
    state = State.new(node_type="writing", base_dir=tmp_path, project_id="p2")
    state.save_artifact("manuscript", "my_paper", "# v1")
    state.node_type = "_orchestrator"
    res = asyncio.run(execute(
        "save_artifact", state,
        artifact_type="manuscript", name="my_paper", content="# v1 边距改了",
    ))
    assert res.get("status") == "success"


def test_orchestrator_can_save_its_own_types(tmp_path):
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p3")
    res = asyncio.run(execute(
        "save_artifact", state,
        artifact_type="compression_log", name="turn_5", content="{}",
    ))
    assert res.get("status") == "success"


def _project_worktree(tmp_path):
    """一个绑得上的 Project v2 worktree（真 git 仓）。"""
    import os
    import subprocess

    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "README.md").write_text("project", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "init"], check=True, env=env)
    return wt


def test_orchestrator_cannot_overwrite_writings_manuscript_but_may_write_its_own(tmp_path):
    """issue #621 的形状在一本账之下：同 id 就是同一份记录（路径即身份），协调者
    拿 writing 的 manuscript 名字 save 不再落成平行身份，而是被**拒绝** ——
    否则就是改写 writing 目录里的文件（写不跨节点）。换一个 name 才是它自己的稿：
    落 notes/、盖 `_orchestrator` 的章；writing 的那份一字不动。两份记录各自说清
    自己是谁写的 —— 账不假。"""
    wt = _project_worktree(tmp_path)
    writing = State.new(node_type="writing", base_dir=tmp_path / "runs",
                        project_id="p621", project_worktree=wt)
    writing.save_artifact("manuscript", "uk_food_culture_paper", "# writing 的正文")
    # 正文就是文件：writing 的稿子落它自己的目录 paper/，manuscript 落 .tex
    own = wt / "paper" / "manuscript__uk_food_culture_paper.tex"
    assert own.exists()
    before = own.read_text(encoding="utf-8")

    orch = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs",
                     project_id="p621", project_worktree=wt)
    refused = asyncio.run(execute(
        "save_artifact", orch,
        artifact_type="manuscript", name="uk_food_culture_paper",
        content="协调者代笔的正文",
    ))
    assert refused.get("status") == "error", refused
    assert "别的节点" in refused["error"] and "换一个 name" in refused["error"]
    assert own.read_text(encoding="utf-8") == before          # writing 的那份没被动
    assert not (wt / "notes" / "manuscript__uk_food_culture_paper.tex").exists()
    assert writing.read_artifact("manuscript__uk_food_culture_paper")["version"] == 1

    res = asyncio.run(execute(
        "save_artifact", orch,
        artifact_type="manuscript", name="orchestrator_draft",
        content="协调者代笔的正文",
    ))
    assert res.get("status") == "success", res
    # 协调者的那份落**它自己的**目录 notes/
    assert (wt / "notes" / "manuscript__orchestrator_draft.tex").exists()
    mine = orch.list_artifacts("manuscript", own_only=True)
    assert [a["id"] for a in mine] == ["manuscript__orchestrator_draft"]
    rec = orch.read_artifact(mine[0]["id"])
    assert rec["produced_by_node_type"] == "_orchestrator"
    assert own.read_text(encoding="utf-8") == before


def test_writings_own_manuscript_is_untouched_by_the_guard(tmp_path):
    wt = _project_worktree(tmp_path)
    writing = State.new(node_type="writing", base_dir=tmp_path / "runs",
                        project_id="p621b", project_worktree=wt)
    writing.save_artifact("manuscript", "paper", "# v1")
    res = asyncio.run(execute(
        "save_artifact", writing,
        artifact_type="manuscript", name="paper", content="# v2",
    ))
    assert res.get("status") == "success", res


def test_producing_node_can_still_create_its_own_output(tmp_path):
    state = State.new(node_type="writing", base_dir=tmp_path, project_id="p4")
    res = asyncio.run(execute(
        "save_artifact", state,
        artifact_type="manuscript", name="new_paper", content="# real paper",
    ))
    assert res.get("status") == "success"


# ── 2：run_node(writing) 不再被 writing-gate 拦 ────────────────────────────

def _dispatch_writing(state: State, monkeypatch) -> tuple[dict, dict]:
    """把派发走到底：execute_node 与 post-producing flow 都换成桩，只看闸。"""
    captured: dict = {}

    async def fake_execute_node(**kw):
        captured["node_type"] = kw["node_type"]
        captured["node_inputs"] = kw.get("node_inputs")
        return {
            "run_id": "fake_writing_run", "node_type": kw["node_type"],
            "project_id": state.project_id, "status": "completed",
            "missing_required_outputs": [], "turns": 1, "tool_call_count": 0,
            "artifacts": [], "final_text_preview": "",
            "state_dir": str(Path(tempfile.mkdtemp())), "project_root": None,
            "depth": 1, "sub_run_id": "t",
        }

    async def no_flow(*a, **k):
        return None

    import core.executor
    monkeypatch.setattr(core.executor, "execute_node", fake_execute_node)
    monkeypatch.setattr("shared.tools.run_node._run_post_producing_flow", no_flow)
    monkeypatch.setattr("core.llm.LLMClient", lambda: MagicMock())
    res = asyncio.run(execute(
        "run_node", state, node_type="writing", user_note="测试派发", node_inputs={},
    ))
    return res, captured


def test_run_node_writing_is_dispatched_without_synthesis(tmp_path, monkeypatch):
    """从前：⛔ writing-gate「还没有 project_synthesis 评估」（fire_data 28 次开火冠军）。
    现在：照派。墙加回去这条转红。"""
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p5")
    state.hook_state["_callable_nodes"] = ["*"]
    res, captured = _dispatch_writing(state, monkeypatch)
    assert "writing-gate" not in (res.get("error") or ""), res
    assert res.get("status") == "success", res
    assert captured["node_type"] == "writing"


def test_run_node_writing_allowed_when_ready(tmp_path, monkeypatch):
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p7")
    state.hook_state["_callable_nodes"] = ["*"]
    state.save_artifact(
        "review_critique", "project_synthesis__p7",
        content='{"project_verdict": "ready_to_write"}',
        metadata={"scope": "project_synthesis", "project_verdict": "ready_to_write"},
        provenance=_review_provenance(state),
    )
    res, captured = _dispatch_writing(state, monkeypatch)
    assert res.get("status") == "success", res
    assert captured["node_type"] == "writing"


def test_the_writing_gate_tooling_is_gone():
    """整套退场：override 工具、pause 类型、注入输入都不在了；纯函数留着。"""
    from core import pause_driver
    from core.loader import load_harness
    from core.pause import PauseEvent
    from shared.tools.library import writing_gate

    assert get_tool("present_writing_gate_override") is None
    assert not hasattr(writing_gate, "record_writing_gate_override_answer")
    assert not hasattr(writing_gate, "writing_override_for")
    assert callable(writing_gate.resolve_project_synthesis)
    # pause_driver 不再把 writing_gate_override 当成一种要倒计时的裁决类型
    pe = PauseEvent(question="q", options=["A", "B"],
                    metadata={"type": "writing_gate_override", "recommended_option_index": 0})
    assert pause_driver.auto_approve_answer(pe) != "1"
    inputs = load_harness("writing").expected_inputs
    assert "writing_gate_mode" not in inputs
    assert "writing_gate_authorization_source" not in inputs
    orch = load_harness("_orchestrator")
    assert "present_writing_gate_override" not in (orch.tools or [])
    assert "forward_artifact" not in (orch.tools or [])


def test_orchestrator_prompt_no_longer_promises_the_wall():
    """文案与判据同源：harness 里不能还写着「会被框架机械拦截」。"""
    text = (Path(__file__).resolve().parent.parent / "nodes" / "_orchestrator"
            / "harness.yaml").read_text(encoding="utf-8")
    assert "present_writing_gate_override(" not in text
    assert "会被框架机械拦截" not in text
    assert "新建会被框架报错拒绝" not in text
    assert "一律被框架**硬拒**" not in text
