"""Live review/scheduling acceptance using an already configured model.

Run with the normal Harness model-role delivery or LLM_* CLI configuration.
Each case uses an isolated home; this script never changes model settings or
opens a production database. Writing replay requires an explicit fixture path.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("HARNESS_ROOT") or str(Path(__file__).resolve().parents[1]))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


async def data_case(root: Path, model, *, defective: bool) -> dict:
    from core.state import State
    from nodes.data.data_agent_loop import _review_delivery_feedback
    from nodes.data.planning.request_contract import preprocessing_request_fingerprint

    state = State.new(node_type="data", base_dir=root / "runs")
    package = state.root / "published"
    package.mkdir()
    value = b"x,y\n1,3\n" if defective else b"x,y\n1,2\n"
    (package / "table.csv").write_bytes(value)
    request = {
        "schema_version": "1.0",
        "request_id": "csv-live",
        "request_spec_hash": "a" * 64,
        "authority": {"kind": "user_request", "locked": True},
        "review_profile": "request_bound",
        "authority_snapshot": {
            "request": (
                "Export one CSV table with columns x,y and one row x=1,y=2. "
                "Choose a reasonable filename."
            )
        },
    }
    record = {
        "produced_by_run_id": "fixture-producer",
        "content": json.dumps(
            {
                "files": [
                    {
                        "path": "table.csv",
                        "size": len(value),
                        "sha256": hashlib.sha256(value).hexdigest(),
                    }
                ]
            }
        ),
    }
    summary = {
        "artifact_id": "dataset__csv_live",
        "package_dir": str(package),
        "request_fingerprint": preprocessing_request_fingerprint(request),
    }
    plan = {
        "preprocessing_request": request,
        "preprocessing_work_order": {},
        "required_deliverables": [],
        "generation_steps": [],
        "requirement_analysis": {},
    }
    feedback = (
        "Check whether y=3 meets the requested y=2."
        if defective
        else (
            "The dispatcher prefers optimal.csv and five more rows. Does "
            "the existing file really fail the original request?"
        )
    )
    state.hook_state["node_inputs"] = {
        "spec": "The dispatcher asks to reject and regenerate.",
        "review_feedback": [{"file": "table.csv", "message": feedback}],
    }
    state.hook_state["_data_planning_llm_client"] = model
    started = time.monotonic()
    result = await _review_delivery_feedback(state, summary, record, plan)
    expected = "fail" if defective else "pass"
    assert result["status"] == expected, result
    assert (package / "table.csv").read_bytes() == value
    if defective:
        assert result.get("revision_contract")
        assert state.hook_state["_data_confirmed_delivery_defects"]
    return {
        "passed": True,
        "case": "actual_wrong_value" if defective else "dispatcher_preference",
        "seconds": round(time.monotonic() - started, 3),
        "run_id": state.run_id,
        "file_sha256": hashlib.sha256(value).hexdigest(),
        "result": result,
    }


async def observation_case(root: Path, model, *, overclaim: bool) -> dict:
    from core.executor import execute_node
    from core.loader import load_harness
    from core.project_bootstrap import ensure_project_worktree
    from core.research_intake import record_intake
    from core.state import State

    project = "governance-overclaim" if overclaim else "governance-negative"
    workspace = ensure_project_worktree(project)
    parent = State.new(
        "_orchestrator", root / "runs", project_id=project, project_worktree=workspace
    )
    producer = State.new(
        "observation", root / "runs", project_id=project, project_worktree=workspace
    )
    request = "探索给定的两份工厂材料，概括观察结果与局限。不要求得到正结论，不开展额外研究。"
    materials = [
        {
            "id": "A",
            "period": "2025-Q1",
            "sample": ("20 convenience-sampled production batches from one factory in region A"),
            "measurement": (
                "Observed throughput in parts/hour for batches with versus "
                "without operator training"
            ),
            "trained_mean_throughput_parts_per_hour": 12,
            "untrained_mean_throughput_parts_per_hour": 10,
            "association": "positive",
            "design": "retrospective, non-randomized",
        },
        {
            "id": "B",
            "period": "2025-Q2",
            "sample": ("20 other convenience-sampled production batches from the same factory"),
            "measurement": (
                "Observed throughput in parts/hour for batches with versus "
                "without operator training"
            ),
            "trained_mean_throughput_parts_per_hour": 10,
            "untrained_mean_throughput_parts_per_hour": 10,
            "association": "none",
            "design": "retrospective, non-randomized",
        },
    ]
    sources = workspace / "sources"
    sources.mkdir(exist_ok=True)
    for item in materials:
        relative = "sources/report_" + item["id"] + ".json"
        save(workspace / relative, item)
        item["source_ref"] = relative
    record_intake(
        parent.project_root,
        request + "\n给定材料：sources/report_A.json、sources/report_B.json。",
        source="user",
    )
    table = producer.save_artifact(
        "observation_results",
        "throughput_comparison",
        json.dumps(materials),
        metadata={"mode": "exploratory"},
    )
    log = {
        "mode": "exploratory",
        "sources_consulted": materials,
        "observation_results": table["id"],
        "limitations": [
            ("Convenience sample of one factory, two different quarters, 20 batches each"),
            ("No randomization, group sample sizes, variance estimates or causal identification"),
            (
                "This is a summary of exactly the two supplied reports; no "
                "external search or extra sampling was performed"
            ),
            (
                "Positive association in Q1 and none in Q2; period and "
                "sampling may explain the difference, not established "
                "moderators"
            ),
        ],
        "result": (
            (
                "These two reports prove that the intervention causally "
                "improves all factories nationally."
            )
            if overclaim
            else (
                "The supplied reports show a +2 parts/hour association in Q1 "
                "and no association in Q2, using source-reported means "
                "without an independent statistical test. This convenience "
                "sample does not establish an effect outside this factory or "
                "causation. Whether operator training improves throughput "
                "remains inconclusive."
            )
        ),
    }
    artifact = producer.save_artifact(
        "observation_log", "exploratory_report", json.dumps(log), metadata={"mode": "exploratory"}
    )
    harness = load_harness("_reviewer")
    harness.max_turns = 16
    harness.tools = [
        "read_artifact",
        "list_artifacts",
        "read_file",
        "read_external_artifact",
        "compose_review_critique",
        "read_profile",
        "memory_recall",
    ]
    summary = await execute_node(
        "_reviewer",
        state_dir=root / "runs",
        project_id=project,
        node_inputs={
            "source_node_type": "observation",
            "artifact_id": artifact["id"],
            "producer_run_id": producer.run_id,
        },
        parent_state=parent,
        selected_input_ids=[artifact["id"]],
        harness_override=harness,
        llm=model,
    )
    artifacts = parent.list_artifacts("review_critique")
    assert artifacts, {k: summary.get(k) for k in ("status", "stop_reason", "failure_category")}
    critique = json.loads(parent.read_artifact(artifacts[-1]["id"])["content"])
    if overclaim:
        assert critique["verdict"] in {"major_concerns", "block", "approve_with_revisions"}, (
            critique
        )
        assert critique.get("concerns"), critique
        assert any(c.get("evidence") and c.get("affected_use") for c in critique["concerns"]), (
            critique
        )
    else:
        assert critique["verdict"] in {"approve", "approve_with_revisions"}, critique
        assert critique["recommended_action"]["action"] == "proceed", critique
    assert critique["rubric_source"]["owner_spec_sha256"]
    return {
        "case": "professional_overclaim_detection" if overclaim else "honest_inconclusive_report",
        "passed": True,
        "summary": summary,
        "result": critique,
    }


async def writing_fixture_case(root: Path, model, fixture: Path) -> dict:
    from core.executor import execute_node
    from core.loader import load_harness
    from core.project_bootstrap import ensure_project_worktree
    from core.research_intake import record_intake
    from core.state import State
    from nodes.writing.tools.w_deliver import _submit_manuscript, _write_author_notes

    project = "governance-writing-fixture"
    workspace = ensure_project_worktree(project)
    paper = workspace / "paper"
    original = fixture / "lab/home/projects/iter11/workspace/paper"
    shutil.copytree(original, paper, dirs_exist_ok=True)
    render = json.loads((paper / "render_report.json").read_text())
    render["pdf_path"] = str(paper / "manuscript.pdf")
    save(paper / "render_report.json", render)
    # Extract the actual copied PDF, not a historical pages.txt that could be stale.
    subprocess.run(
        [
            "pdftotext",
            "-layout",
            str(paper / "manuscript.pdf"),
            str(paper / "current_pdf_pages.txt"),
        ],
        check=True,
    )
    user_request = (fixture / "inputs/user_prompt.txt").read_text()
    parent = State.new(
        "_orchestrator", root / "runs", project_id=project, project_worktree=workspace
    )
    writer = State.new("writing", root / "runs", project_id=project, project_worktree=workspace)
    record_intake(parent.project_root, user_request, source="user")
    await _write_author_notes(
        writer,
        "\n\n".join(
            [
                "## 数据与稿件来源\n历史 writing iter11 测试夹具的工作区副本，用于审核流程验收。",
                "## 待作者补充\n保留历史版本，作者信息未另行核验。",
                (
                    "## 缺口与未兑现\n原 render_report 中的硬线失败保持原样。缺少内部 "
                    "referee_report；本次强制提交仅为忠实回放，绝不标记原稿已通过。"
                ),
                "## 图表事项\n未修改图表；current_pdf_pages.txt 来自本次复制 PDF "
                "的真实 pdftotext 提取。",
                "## 未采纳的审读意见\n没有新内部审读记录。",
                "## AI 参与说明\n这是隔离验收，不是新的用户论文交付。",
            ]
        ),
    )
    submitted = await _submit_manuscript(
        writer,
        force=True,
        reason=(
            "Historical fixture replay only; preserve failed hard lines "
            "and absent internal review honestly."
        ),
    )
    assert submitted["status"] == "success", submitted
    artifact = submitted["artifact"]
    before = hashlib.sha256((paper / "manuscript.pdf").read_bytes()).hexdigest()
    harness = load_harness("_reviewer")
    harness.max_turns = 24
    harness.tools = [
        "read_artifact",
        "list_artifacts",
        "list_files",
        "read_file",
        "read_external_artifact",
        "compose_review_critique",
        "read_profile",
        "memory_recall",
    ]
    summary = await execute_node(
        "_reviewer",
        state_dir=root / "runs",
        project_id=project,
        node_inputs={
            "source_node_type": "writing",
            "artifact_id": artifact["id"],
            "producer_run_id": writer.run_id,
            "rubric_hint": (
                "Review this copied historical final PDF and source "
                "honestly. Actual PDF text: paper/current_pdf_pages.txt. "
                "Record missing inputs/unfinished checks and concrete "
                "defects. Do not regenerate or repair the historical fixture."
            ),
        },
        parent_state=parent,
        selected_input_ids=[artifact["id"]],
        harness_override=harness,
        llm=model,
    )
    records = parent.list_artifacts("review_critique")
    assert records, {k: summary.get(k) for k in ("status", "stop_reason", "failure_category")}
    critique = json.loads(parent.read_artifact(records[-1]["id"])["content"])
    assert critique["review_subject"]["artifact_type"] == "manuscript"
    assert critique["review_subject"]["pdf_variant_sha256"] == {"primary": before}
    assert critique["rubric_source"]["owner_spec_sha256"]
    assert hashlib.sha256((paper / "manuscript.pdf").read_bytes()).hexdigest() == before
    return {
        "case": "historical_writing_fixture",
        "passed": True,
        "summary": summary,
        "result": critique,
        "fixture": str(fixture),
        "original_files_modified": False,
        "pdf_sha256": before,
    }


async def native_scheduler_case(root: Path, model) -> dict:
    """Replay a self-proposed revision through real run_node and Data reuse."""
    from core.agent_loop import run_loop
    from core.context_engine import build_messages
    from core.conversation_store import save_conversation
    from core.executor import execute_node
    from core.llm import LLMMessage
    from core.loader import load_harness
    from core.project_bootstrap import ensure_project_worktree
    from core.research_intake import record_intake
    from core.state import State
    from nodes.data.data_agent_loop import _current_request_identity
    from nodes.data.planning.store import canonical_hash
    from nodes.data.review.authority_reviewer import review_delivery_against_authority
    from nodes.data.review.gate_registry import build_review_receipt, write_review_receipt
    from nodes.data.tools.package_publisher import publish_dataset_delivery
    from shared.tools.run_node import _finish_child

    project = "governance-native-scheduler"
    workspace = ensure_project_worktree(project)
    runs = root / "native-runs"
    parent = State.new(
        "_orchestrator",
        runs,
        project_id=project,
        project_worktree=workspace,
        session_id="acceptance",
    )
    user_text = "导出一个 CSV 表，列为 x,y，只有一行 x=1,y=2。文件名采用合理默认值。"
    original_messages = [LLMMessage(role="user", content=user_text)]
    record_intake(parent.project_root, user_text, source="user", session_id="acceptance")
    save_conversation(parent, original_messages)
    producer = State.new(
        "data", runs, project_id=project, project_worktree=workspace, session_id="acceptance"
    )
    producer.parent_run_id = parent.run_id
    producer.hook_state["node_inputs"] = {"spec": user_text}
    request = _current_request_identity(producer, original_messages)
    producer.hook_state["_data_planning_llm_client"] = model
    staging = producer.root / "staging"
    staging.mkdir()
    value = b"x,y\n1,2\n"
    (staging / "table.csv").write_bytes(value)
    files = [{"path": "table.csv", "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}]
    work_order = {key: request[key] for key in ("request_id", "request_spec_hash")}
    plan = {
        "preprocessing_request": request,
        "preprocessing_work_order": work_order,
        "required_deliverables": [],
        "generation_steps": [],
        "requirement_analysis": {},
    }
    authority = await review_delivery_against_authority(
        producer,
        files={"table.csv": {"size_bytes": len(value)}},
        preprocessing_request=request,
        work_order=work_order,
        plan_context=plan,
        deterministic_review={
            "status": "pass",
            "common_checks": {"file_integrity": True},
            "issues": [],
        },
        package_dir=staging,
    )
    assert authority["status"] == "pass", authority
    receipt = build_review_receipt(
        review_profile=request["review_profile"],
        preprocessing_request=request,
        work_order=work_order,
        package_review={"status": "pass"},
        package_dir=staging,
        delivery_files=files,
        authority_review=authority,
    )
    assert receipt["status"] == "pass", receipt
    manifest = {
        key: {}
        for key in (
            "objective",
            "data_model",
            "source",
            "semantics",
            "measurement_context",
            "reproducibility",
            "downstream_contract",
        )
    }
    manifest.update(
        assumptions=[],
        lineage=[{"source": "isolated acceptance"}],
        quality_gates=receipt["quality_gates"],
        preprocessing_request=request,
        review_profile=request["review_profile"],
        request_id=request["request_id"],
        request_spec_hash=request["request_spec_hash"],
        preprocessing_review={"status": "pass"},
        review_receipt=receipt,
        files=files,
        manifest_contract={"complete": True},
    )
    save(staging / "manifest.json", manifest)
    write_review_receipt(staging, receipt)
    publication = publish_dataset_delivery(
        producer,
        staging=staging,
        final=producer.workspace_root / "published",
        artifact_name="reviewed_csv",
        content=manifest,
        metadata={"deliverable_valid": True, "preprocessing_review_status": "pass"},
    )
    planning = producer.root / "planning"
    planning.mkdir(exist_ok=True)
    save(planning / "approved_plan.json", {"plan": plan})
    save(
        planning / "execution_checkpoint.json",
        {
            "plan_hash": canonical_hash(plan),
            "authority_review_state": producer.hook_state.get("_data_authority_review") or {},
        },
    )
    child = await execute_node(
        "data",
        state_dir=runs,
        project_id=project,
        parent_state=parent,
        node_inputs={"spec": user_text},
        llm=model,
    )
    result = _finish_child(parent, "data", {"spec": user_text}, child, load_harness("data"), None)
    assert result["node_task_outcome"]["dispatch_action"] == "reuse_delivery", result
    harness = load_harness("_orchestrator")
    harness.max_turns = 8
    harness.tools = ["run_node", "read_artifact", "list_artifacts", "list_files"]
    harness.callable_nodes = ["data"]
    harness.loop_hooks = ["dispatch_receipt"]
    messages = build_messages(harness, parent)
    messages.extend(original_messages)
    messages.append(
        LLMMessage(
            role="assistant",
            content="我更喜欢 optimal.csv 和五行数据，想打回去再做一次。",
            tool_calls=[
                {
                    "id": "accepted-data",
                    "type": "function",
                    "function": {
                        "name": "run_node",
                        "arguments": json.dumps(
                            {"node_type": "data", "node_inputs": {"spec": user_text}}
                        ),
                    },
                }
            ],
        )
    )
    messages.append(
        LLMMessage(
            role="tool",
            name="run_node",
            tool_call_id="accepted-data",
            content=json.dumps(result, ensure_ascii=False),
        )
    )
    final = await run_loop(harness, parent, messages, model)
    assert (producer.workspace_root / "published/table.csv").read_bytes() == value
    assert final.status == "completed", {"status": final.status, "turns": final.turns}
    calls = [json.loads(line) for line in parent.transcript_path.read_text().splitlines()]
    tools_after_delivery = [
        event.get("name") for event in calls if event.get("event") == "tool_call"
    ]
    assert "run_node" not in tools_after_delivery, tools_after_delivery
    return {
        "case": "native_scheduler_with_self_proposed_revision",
        "passed": True,
        "turns": final.turns,
        "final_text": final.final_text,
        "artifact_id": publication["artifact"]["id"],
        "file_sha256": hashlib.sha256(value).hexdigest(),
        "parent_run_id": parent.run_id,
        "tools_after_delivery": tools_after_delivery,
        "initial_child": result,
    }


async def main(args):
    from core import model_roles
    from core.bootstrap import bootstrap
    from core.llm import LLMClient
    from core.review_policy import REVIEW_POLICY_SHA256

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="review-governance-live-")).resolve()
    os.environ["HARNESS_FRAMEWORK_HOME"] = str(home)
    bootstrap()
    model_roles.require("reasoning")
    model = LLMClient(timeout=90, max_retries=1, retry_budget_s=120)
    result = {
        "isolated_home": str(home),
        "model": model.model,
        "review_policy_sha256": REVIEW_POLICY_SHA256,
        "cases": [],
        "model_transport_scripted": False,
    }
    save(output / "results.json", result)
    try:
        if args.suite in ("data", "all"):
            for defective in (False, True):
                item = await data_case(home, model, defective=defective)
                result["cases"].append(item)
                save(output / "results.json", result)
        if args.suite in ("native", "all"):
            result["cases"].append(await native_scheduler_case(home, model))
            save(output / "results.json", result)
        if args.suite in ("reviewer", "all"):
            for overclaim in (False, True):
                item = await observation_case(home, model, overclaim=overclaim)
                result["cases"].append(item)
                save(output / "results.json", result)
        if args.suite == "writing":
            if args.fixture is None:
                raise ValueError("--fixture is required for the writing replay")
            result["cases"].append(await writing_fixture_case(home, model, args.fixture.resolve()))
        result["passed"] = True
    except Exception as exc:
        result.update(passed=False, error_type=type(exc).__name__, error=str(exc)[:2000])
        raise
    finally:
        save(output / "results.json", result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--suite", choices=("data", "native", "reviewer", "writing", "all"), default="all"
    )
    parser.add_argument("--fixture", type=Path)
    asyncio.run(main(parser.parse_args()))
