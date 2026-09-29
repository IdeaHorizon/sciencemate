"""provider 空停跑到一半才发生时，框架要认账（issue #253 / #250）。

jicq E2E 实测（5 个 project 复现 2 个，backend=deepseek-v4-pro）：

  Study1 跑 8 轮全在检索、Study3 跑 6 轮全在检索，最后一轮
  `finish_reason=stop` + `content=""` + `tool_calls=[]`，
  classify_papers / archive_papers / save_artifact 一个没执行，
  survey_report 和 literature_index 双缺。

agent_loop 这一层已经有空轮事务性重试（回滚 messages + 退避重放，用尽后
`LoopResult.status="void"`）。缺的是**下游没人消费这个结论**：

  - executor 的失败分类器第一句就是 `if records: return None, None`
    → 跑过 8 轮检索的 run 永远命不中 blank_stop → summary 里没有归因，
      看起来就是"literature 节点质量不行"。
  - run_node 的基础设施重试判据是 `tool_call_count == 0 and turns <= 2`
    → 同样把它挡在门外，于是 provider 打嗝被原样当节点失败交回 orchestrator。
  - chat 的 paused 分支拿到空回复就回"（节点已处理完，但没有返回额外说明。）"
    → decision gate 答完之后收到这句，人看到的是"选了没反应"（#250）。

三处是同一条缝的三个出口：**机制存在，结论没接到路径上**。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.agent_loop import LoopResult
from core.bootstrap import bootstrap
from core.executor import (
    _classify_incomplete_failure_detail,
    finalize_run,
)
from core.harness import NodeHarness
from core.llm import LLMClient
from core.pause import clear_all
from core.state import State


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield
    clear_all()


# 8 轮检索的现场：只读工具调了一堆，什么都没落地
_SEARCH_RECORDS = [
    {"id": f"c{i}", "function": {"name": "search_papers"},
     "result": {"status": "success", "count": 5}}
    for i in range(13)
]


# ── #253-A：分类器要认"跑到一半的空停" ────────────────────────────────────

def test_void_after_many_tool_calls_is_blank_stop():
    """核心回归：13 次只读调用之后的空停，仍然是 provider 协议故障。"""
    lr = LoopResult(final_text="", turns=8, tool_calls=_SEARCH_RECORDS,
                    status="void")
    cat, sub = _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["survey_report", "literature_index"])
    assert cat == "provider_tool_call_protocol_error"
    assert sub == "blank_stop"


def test_void_with_outputs_complete_is_not_infra():
    """产出齐了（只是 QC 挂）→ 不许赖到 provider 头上。"""
    lr = LoopResult(final_text="", turns=8, tool_calls=_SEARCH_RECORDS,
                    status="void")
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=[]) == (None, None)


def test_normal_finish_with_tool_calls_still_not_infra():
    """不误伤：模型正常收工（status=completed）、只是没产出 → 仍归节点质量。

    这条守着上面那个新入口不能退化成"只要缺产出就赖 provider"。
    """
    lr = LoopResult(final_text="", turns=8, tool_calls=_SEARCH_RECORDS,
                    status="completed")
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["survey_report"]) == (None, None)


def test_blocked_run_with_text_not_infra():
    """不误伤：说了人话的 blocked run（"材料不足…"）不是协议故障。"""
    lr = LoopResult(final_text="材料不足，无法继续检索，请补充关键词。",
                    turns=3, tool_calls=_SEARCH_RECORDS, status="completed")
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["survey_report"]) == (None, None)


@pytest.mark.asyncio
async def test_finalize_run_writes_blank_stop_for_void(tmp_path: Path):
    """端到端：summary.json 里要能查到归因，不能只留一个光秃秃的 incomplete。"""
    state = State.new(node_type="literature", base_dir=tmp_path)
    harness = NodeHarness(
        node_type="literature", system_prompt="t", tools=[], max_turns=30,
        required_outputs=["survey_report"],
    )
    lr = LoopResult(final_text="", turns=8, tool_calls=_SEARCH_RECORDS,
                    status="void")
    summary = await finalize_run(state, harness, lr, LLMClient(
        api_key="x", base_url="http://localhost", model="m"))
    assert summary["status"] == "incomplete"
    assert summary["failure_category"] == "provider_tool_call_protocol_error"
    assert summary["failure_subcategory"] == "blank_stop"


# ── #253-B：重试判据问的是"有没有产出"，不是"调没调工具" ──────────────────

@pytest.mark.asyncio
async def test_summary_exposes_own_produced_artifacts(tmp_path: Path):
    """summary 要带上"本 run 自己落地了什么"——重试判据的唯一权威依据。"""
    state = State.new(node_type="literature", base_dir=tmp_path)
    harness = NodeHarness(
        node_type="literature", system_prompt="t", tools=[], max_turns=30,
        required_outputs=["survey_report"],
    )
    llm = LLMClient(api_key="x", base_url="http://localhost", model="m")

    lr = LoopResult(final_text="", turns=8, tool_calls=_SEARCH_RECORDS,
                    status="void")
    empty = await finalize_run(state, harness, lr, llm)
    assert empty["produced_artifact_types"] == [], "只检索没落地 = 零产出"

    state.save_artifact("survey_report", "s", "内容")
    done = await finalize_run(state, harness, lr, llm)
    assert done["produced_artifact_types"] == ["survey_report"]


def test_infra_retry_asks_about_output_not_tool_count():
    """13 次只读检索 + 零产出 → 值得重试（重跑毁不掉任何东西）。"""
    from shared.tools.run_node import _is_retryable_infra_failure

    base = {"status": "incomplete", "turns": 8, "tool_call_count": 13,
            "failure_category": "provider_tool_call_protocol_error"}
    assert _is_retryable_infra_failure({**base, "produced_artifact_types": []})
    assert not _is_retryable_infra_failure(
        {**base, "produced_artifact_types": ["survey_report"]}), \
        "已经落了 artifact → 重跑会浪费/覆盖成果"
    assert not _is_retryable_infra_failure(
        {**base, "produced_artifact_types": [],
         "failure_category": "quality_checks_failed"}), "真失败不能被吞掉"
    assert not _is_retryable_infra_failure(
        {**base, "produced_artifact_types": [], "status": "completed"})


def test_infra_retry_falls_back_when_field_absent():
    """老 summary 没这个字段 → 退回旧代理量，保守不放宽。"""
    from shared.tools.run_node import _is_retryable_infra_failure

    legacy = {"status": "incomplete", "turns": 1, "tool_call_count": 0,
              "failure_category": "provider_tool_call_protocol_error"}
    assert _is_retryable_infra_failure(legacy)
    assert not _is_retryable_infra_failure({**legacy, "tool_call_count": 12})


# ── #250：decision gate 答完之后的空轮不许谎报"已处理完" ───────────────────

def _reply_after_pause(monkeypatch, state, *, void: bool) -> str:
    """跑 chat 的 paused 分支：resume 之后模型回了空。"""
    import chat as chat_mod
    from core import pause_driver

    async def _fake_drive(**_kw):
        return ""              # 顶层 final_text 为空 —— 正是现场
    monkeypatch.setattr(pause_driver, "drive_pause_chain", _fake_drive)
    if void:
        state.hook_state["_void_turn_final"] = {
            "turn": 12, "retries": 3, "prompt_tokens": 96000,
            "completion_tokens": 0, "leak_kind": "empty",
        }
    result = LoopResult(final_text="", turns=12, tool_calls=[], status="paused")

    async def _ask(_pe):        # drive_pause_chain 已被替换，问答函数不会被调
        return ""
    return asyncio.run(chat_mod._post_loop_reply(
        result, None, state, [], None, ask_pause=_ask))


def test_void_after_decision_gate_is_not_reported_as_processed(
        tmp_path, monkeypatch):
    """现场（#250 案例 1/2）：答完 decision gate 收到"节点已处理完" = 假话。"""
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    reply = _reply_after_pause(monkeypatch, state, void=True)
    assert "已处理完" not in reply, "什么都没执行，不许说处理完了"
    assert "空响应" in reply
    assert "还没有被执行" in reply


def test_void_marker_survives_for_continuous_replay(tmp_path, monkeypatch):
    """peek 不 pop：continuous 驱动层还要靠这个标记安排驻定重放。"""
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    _reply_after_pause(monkeypatch, state, void=True)
    assert state.hook_state.get("_void_turn_final"), \
        "被提前 pop 掉的话，continuous 模式就不重放了"


def test_plain_empty_reply_keeps_old_wording(tmp_path, monkeypatch):
    """不误伤：不是空轮的空回复（节点真跑完了没话说）保持原文案。"""
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    reply = _reply_after_pause(monkeypatch, state, void=False)
    assert reply == "（节点已处理完，但没有返回额外说明。）"
