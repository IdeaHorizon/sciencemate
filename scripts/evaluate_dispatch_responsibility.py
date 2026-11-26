"""Live parent-routing acceptance for #1390, with isolated fixture artifacts.

Uses the normal configured reasoning model and production orchestrator context,
situation hook and tool schema. No child research or external search is run.
The prompts replay the inputs quoted in #1390, not the unavailable full archive.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.environ.get("HARNESS_ROOT") or str(Path(__file__).resolve().parents[1]))


def cases():
    return [
        {
            "name": "first_fulltext",
            "expected": "literature",
            "request": '执行冻结 prereg v3 的 S0–S2b：建立 LMA '
            '/ LDAR / SAFIR / 宽带 VHF 干涉定位文献的'
            "逐篇 8 字段证据表（先导类型 / 速度值 / 样本量 / 测量方法 / 分析窗 / 误差处理 /"
            "授时方案 / 是否检验站间相关）。本轮先完成 S0，至少核验 12 篇全文的原句和页码。",
        },
        {
            "name": "continue_unpaywall",
            "expected": "literature",
            "previous": True,
            "request": '续跑 S0，只做两件事，其余（39 行证据表、报告'
            '正文、三值判定、个例归因、Jordan 锚点替换）'
            "一律不动：补全文通道，目标把全文级从 2 篇提到至少 12 篇。上一轮 6 轮通道全部失败，"
            "但有一条通道没试：api.unpaywall.org。沿用已有索引和缺口，不重新开题。",
        },
        {
            "name": "continue_publisher",
            "expected": "literature",
            "previous": True,
            "request": '续跑 S0，只做一件事：R8 直连 publishe'
            'r 主机重试全文。其余（39 行证据表结构、三值判定、'
            "个例归因、Jordan 锚点、已通过的 8 条验收）一律不动。R7 已把 20 篇 is_oa=true 的"
            "OA 位置拿到，复用先前产物，只填原句和页码缺口。",
        },
        {
            "name": "experiment_with_method_dependency",
            "expected": "experiment",
            "request": "本轮交付是在给定软件和给定数据上运行定位求解器，"
            "输出真实残差、授时扰动下的数值误差和"
            "实验记录。若缺少论文中的一个方法阈值，定向查证后继续实验；不要把实验整项改成综述。",
        },
        {
            "name": "data_primary",
            "expected": "data",
            "request": '本轮只把提供的三份 CSV 合并、校验单位、'
            '记录缺失值，交付可复用的正式数据包及来源清单。'
            "不运行求解器，也不作科学结论。",
        },
        {
            "name": "analysis_primary",
            "expected": "analysis",
            "request": "本轮基于已经核验的证据和实验记录，对速度假说是否得到支持作科学裁决，"
            "更新研究状态。"
            "不新增采集或计算；不是只运行一个统计脚本。",
        },
        {
            "name": "writing_primary",
            "expected": "writing",
            "request": "本轮只根据已冻结的实验记录、图表和证据包撰写可投稿论文。"
            "保留现有研究结论，不重跑实验。",
        },
    ]


async def route(root, item, model):
    from core.agent_loop import run_loop
    from core.bootstrap import bootstrap
    from core.context_engine import build_messages
    from core.ledger import write_record
    from core.llm import LLMMessage
    from core.loader import load_harness
    from core.project_bootstrap import ensure_project_worktree
    from core.state import State

    bootstrap()
    worktree = ensure_project_worktree("dispatch-" + item["name"])
    parent = State.new(
        "_orchestrator",
        root / "runs",
        project_id=item["name"],
        session_id=item["name"],
        project_worktree=worktree,
    )
    index = write_record(
        worktree,
        artifact_type="literature_evidence_package",
        name="s0_index",
        content="Acceptance fixture: 38 indexed entries, full-text gaps remain. "
        "This is a fixture, not real scientific evidence.",
        directory="literature",
        produced_by_node_type="literature",
        produced_by_run_id="fixture-literature",
    )
    write_record(
        worktree,
        artifact_type="research_state",
        name="research_state",
        content=(
            "S0: literature_fulltext_retrieval; S1: solver execution; S2: scientific judgement."
            if item["expected"] == "literature"
            else "Earlier stages and their reviews are complete. Current task: " + item["request"]
        ),
        directory="plan",
        metadata={"verdict": "continue", "version": 1},
        produced_by_node_type="analysis",
        produced_by_run_id="fixture-analysis",
    )
    prereg = write_record(
        worktree,
        artifact_type="pre_registration",
        name="plan",
        content="Acceptance fixture plan: S0 literature_fulltext_retrieval; "
        "S1 execute the supplied solver with supplied data; S2 scientific judgement.",
        directory="plan",
        produced_by_node_type="analysis",
        produced_by_run_id="fixture-analysis",
        frozen=True,
    )
    harness = load_harness("_orchestrator")
    harness.tools = [
        "run_node",
        "task",
        "read_artifact",
        "list_artifacts",
        "read_file",
        "list_files",
    ]
    harness.loop_hooks = ["situation_router", "dispatch_receipt"]
    harness.max_turns = 10
    harness.temperature = 0
    harness.max_tokens = 16000
    parent.hook_state["_callable_nodes"] = list(harness.callable_nodes)
    messages = build_messages(harness, parent)
    if item.get("previous"):
        messages += [
            LLMMessage(
                role="assistant",
                tool_calls=[
                    {
                        "id": "prior-experiment",
                        "type": "function",
                        "function": {
                            "name": "run_node",
                            "arguments": json.dumps(
                                {
                                    "node_type": "experiment",
                                    "node_inputs": {
                                        "experiment_spec": "S0: retrieve and verify full texts."
                                    },
                                }
                            ),
                        },
                    }
                ],
            ),
            LLMMessage(
                role="tool",
                tool_call_id="prior-experiment",
                name="run_node",
                content=json.dumps(
                    {
                        "status": "success",
                        "child_node_type": "experiment",
                        "child_status": "completed",
                        "final_text_preview": 'Full-text verification remains 3/1'
                        '2; use the existing index and gaps.',
                        "artifacts": [{"id": index["id"], "type": "literature_evidence_package"}],
                    }
                ),
            ),
        ]
    situation = (
        "当前计划 S0=literature_fulltext_retrieval，已有 38 篇索引，尚缺全文原句和页码。"
        if item["expected"] == "literature"
        else '本轮前置材料、已有审核和父流程均已完成；这是'
        '现在要安排的主交付，不是回到 S0 重新调研。'
    )
    messages.append(
        LLMMessage(
            role="user",
            content=situation
            + f"已有冻结预注册：{prereg['id']}（可用 task(contract) 从冻结 head 绑定）。"
            + f"已有索引产物 ID：{index['id']}。\n本轮任务：{item['request']}\n"
            '请选择本轮承办节点，直接用 run_node 派'
            '发实际任务并转交可复用的已有产物；不要只回答计划。',
        )
    )
    execution = {}
    try:
        await run_loop(harness, parent, messages, model)
    except DispatchSelected as chosen:
        execution = chosen.execution
    events = [json.loads(line) for line in parent.transcript_path.read_text().splitlines()]
    calls = [
        e.get("args", {})
        for e in events
        if e.get("event") == "tool_call" and e.get("name") == "run_node"
    ]
    selected = execution.get("node_type")
    forwarded = index["id"] in (calls[-1].get("forward_artifact_ids") or []) if calls else False
    result = {
        "case": item["name"],
        "expected": item["expected"],
        "selected": selected,
        "passed": selected == item["expected"] and (forwarded or selected != "literature"),
        "forwarded_index": forwarded,
        "tool_calls": calls,
        "node_inputs": execution.get("node_inputs"),
        "parent_run_id": parent.run_id,
    }
    return result


class DispatchSelected(BaseException):
    def __init__(self, execution):
        self.execution = execution


async def metadata_revision(root, model):
    from core.context_engine import build_messages
    from core.llm import LLMMessage
    from core.loader import load_harness
    from core.state import State
    from core.tool_registry import execute, get_tool, to_openai_schema

    state = State.new("experiment", root / "metadata-runs")
    source = state.root / "evidence.csv"
    original = b"reference,year\nfixture-A,2020\n"
    source.write_bytes(original)
    target = state.root / "corrected.csv"
    request = {
        "mode": "revise",
        "experiment_spec": 'R8: retry publisher full texts for twel'
        've papers, use Unpaywall and web_fetch.',
        "reviewer_feedback": 'Only correct fixture-A year from 202'
        '0 to 2021 using the existing table. '
        "No network retrieval or recomputation is needed.",
    }
    messages = build_messages(load_harness("experiment"), state, request)
    messages.append(
        LLMMessage(
            role="user",
            content=f"已有证据表 {source} 的完整内容是：\n{original.decode()}\n"
            f"修订副本写到 {target}，原表保留。处理本轮纠错即可。",
        )
    )
    schemas = [
        to_openai_schema(get_tool(name)) for name in ("read_file", "safe_write_file", "web_fetch")
    ]
    chosen = None
    calls = []
    for _ in range(4):
        response = await model.chat(messages, tools=schemas, max_tokens=16000, temperature=0)
        messages.append(
            LLMMessage(role="assistant", content=response.content, tool_calls=response.tool_calls)
        )
        for call in response.tool_calls:
            name = call["function"]["name"]
            arguments = json.loads(call["function"]["arguments"])
            calls.append({"name": name, "args": arguments})
            if name == "web_fetch":
                return {"case": "metadata_revision", "passed": False, "tool_calls": calls}
            if name == "safe_write_file":
                chosen = arguments
                break  # observe the real edit choice before any write occurs
            result = await execute(name, state, **arguments)
            messages.append(
                LLMMessage(
                    role="tool",
                    tool_call_id=call["id"],
                    name=name,
                    content=json.dumps(result, ensure_ascii=False),
                )
            )
        if chosen is not None:
            break
    assert source.read_bytes() == original
    return {
        "case": "metadata_revision",
        "passed": bool(chosen)
        and chosen.get("content") == "reference,year\nfixture-A,2021\n"
        and str(chosen.get("path")) == str(target),
        "tool_calls": calls,
        "original_unchanged": True,
        "network_executed": False,
    }


async def main(args):
    from unittest.mock import patch

    from core.llm import LLMClient

    root = Path(tempfile.mkdtemp(prefix="dispatch-responsibility-"))
    os.environ["HARNESS_FRAMEWORK_HOME"] = str(root)
    os.environ["HARNESS_FRAMEWORK_ORG_HOME"] = str(root / "org")
    args.output.mkdir(parents=True, exist_ok=True)
    model = LLMClient(timeout=90, max_retries=1, retry_budget_s=120)
    picked = [c for c in cases() if not args.cases or c["name"] in args.cases.split(",")]
    semaphore = asyncio.Semaphore(3)

    async def run(item):
        async with semaphore:
            value = await route(root, item, model)
            (args.output / (item["name"] + ".json")).write_text(
                json.dumps(value, ensure_ascii=False, indent=2)
            )
            print(json.dumps({k: value[k] for k in ("case", "selected", "passed")}), flush=True)
            return value

    async def stop_at_child_boundary(**execution):
        raise DispatchSelected(execution)

    with patch("core.executor.execute_node", stop_at_child_boundary):
        results = await asyncio.gather(*(run(item) for item in picked))
    if not args.cases or "metadata_revision" in args.cases.split(","):
        revision = await metadata_revision(root, model)
        results.append(revision)
        print(json.dumps({k: revision[k] for k in ("case", "passed")}), flush=True)
    report = {
        "model": model.model,
        "isolated_home": str(root),
        "research_executed": False,
        "passed": all(r["passed"] for r in results),
        "cases": results,
    }
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", default="")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
