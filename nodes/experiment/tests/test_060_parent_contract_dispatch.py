"""Parent task revisions reach fresh Experiment runs through the real dispatcher.

These tests cover blocked dispatch, bounded local scientific delivery, and the
known manifest-only completion gap.  They keep the production task tool,
run_node dispatcher, executor, agent loop, Experiment tools, and sandbox in the
path; only the external LLM response is scripted.  They are not UI, real-model,
or scheduler acceptance.  The contract writer proves which revision the parent
selected, not that a human authorized a scientific deviation.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.bootstrap import bootstrap
from core.ledger import record_version
from core.llm import LLMClient, LLMResponse
from core.state import State
from core.tool_registry import execute as execute_tool
from nodes.experiment.tools import (
    contract_audit,
    delivery_transaction,
    ledger,
    run_bootstrap,
)


class KnownManifestOnlyCompletionGap(AssertionError):
    """Exact known gap: a framework-only manifest advances an empty run."""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text(
        "schema_version: 2\nname: Parent contract dispatch\n",
        encoding="utf-8",
    )
    for directory in (
        "literature",
        "plan",
        "data",
        "experiments",
        "figures",
        "paper",
        "notes",
    ):
        target = root / directory
        target.mkdir()
        (target / "README.md").write_text(f"# {directory}\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "Initialize Project")
    return root


def _frozen_prereg(
    state: State,
    *,
    value: int,
    amendment_reason: str | None = None,
) -> dict:
    saved = state.save_artifact(
        "pre_registration",
        "parent_selected_square",
        f"# Parent-selected preregistration\n\nvalue={value}\n",
        metadata={
            "execution_contract": {
                "version": 1,
                "scientific_params": {"value": value},
                "runtime_params": {},
            }
        },
        amendment_reason=amendment_reason,
    )
    frozen = state.mark_frozen(
        saved["id"],
        {"freeze_reason": f"formal parent fixture v{saved['version']}"},
    )
    return {
        "artifact_id": saved["id"],
        "version": record_version(frozen),
        "content_hash": frozen["content_hash"],
    }


async def _initial_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    project_id: str,
    max_turns: int = 1,
) -> dict:
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "harness-home"))
    monkeypatch.setenv("HARNESS_DEFAULT_MAX_TURNS", str(max_turns))
    bootstrap()

    worktree = _worktree(tmp_path)
    session_id = f"{project_id}-session"
    parent = State.new(
        node_type="_orchestrator",
        base_dir=tmp_path / "runs",
        project_id=project_id,
        session_id=session_id,
        project_worktree=worktree,
    )
    parent.hook_state["_callable_nodes"] = ["experiment"]
    producer = State.new(
        node_type="hypothesis",
        base_dir=tmp_path / "producer-runs",
        project_id=project_id,
        session_id="hypothesis-producer-v1",
        project_worktree=worktree,
    )
    binding = _frozen_prereg(producer, value=7)

    created = await execute_tool(
        "task",
        parent,
        action="create",
        title="Run the parent-selected square calculation",
        description="Exercise exact task revision dispatch into Experiment.",
    )
    assert created["status"] == "success", created
    contract = await execute_tool(
        "task",
        parent,
        action="contract",
        task_id=created["task"]["id"],
        objective="Use preregistration version one",
        intended_use="dispatch integration evidence",
        target_node="experiment",
        prereg_artifact_id=binding["artifact_id"],
        prereg_version=str(binding["version"]),
        prereg_content_hash=binding["content_hash"],
        reason="initial formal dispatch",
    )
    assert contract["status"] == "success", contract
    return {
        "worktree": worktree,
        "session_id": session_id,
        "parent": parent,
        "task_id": created["task"]["id"],
        "task_instance_uuid": created["task_instance_uuid"],
        "binding": binding,
        "contract": contract,
    }


def _events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _child_view(result: dict, *, session_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        root=Path(result["child_state_dir"]),
        run_id=result["child_run_id"],
        session_id=session_id,
    )


def _assert_child_authority(
    child: SimpleNamespace,
    *,
    task_instance_uuid: str,
    revision: int,
    digest: str,
    binding: dict,
    expected_action_count: int = 0,
) -> list[dict]:
    child_events = _events(child.root / "transcript.jsonl")
    run_start = next(row for row in child_events if row.get("event") == "run_start")
    assert run_start["task_instance_uuid"] == task_instance_uuid
    assert run_start["task_contract_revision"] == revision
    assert run_start["task_contract_digest"] == digest
    assert isinstance(run_start["parent_dispatch_id"], str)
    assert run_start["parent_dispatch_id"].startswith("disp_")

    records = ledger.open_ledger(child).records()
    authority = run_bootstrap.project_run_authority(records)
    assert authority is not None
    assert authority["task_contract"] == {
        "task_instance_uuid": task_instance_uuid,
        "digest": digest,
        "revision": revision,
    }
    assert authority["prereg_status"] == "bound_valid"
    assert authority["prereg"] == binding
    assert authority["startup_blocker"] is None
    all_actions = [row for row in records if row.get("kind") == "action_started"]
    experiment_actions = [
        row for row in all_actions if row.get("tool") != "finish.check"
    ]
    assert len(experiment_actions) == expected_action_count, experiment_actions
    if expected_action_count == 0:
        assert all_actions == []
    return records


def _assert_child_selected(
    result: dict,
    *,
    task_instance_uuid: str,
    revision: int,
    digest: str,
    binding: dict,
    session_id: str,
) -> tuple[SimpleNamespace, list[dict]]:
    # This fixture takes the node's existing structured blocked exit.  A
    # successful dispatch is not evidence of a scientific completion.
    assert result["child_status"] == "blocked", result
    assert result["can_start_standard_review"] is False, result

    child = _child_view(result, session_id=session_id)
    records = _assert_child_authority(
        child,
        task_instance_uuid=task_instance_uuid,
        revision=revision,
        digest=digest,
        binding=binding,
    )
    return child, records


def _completed_scientific_child(
    parent: State,
    *,
    worktree: Path,
    task_instance_uuid: str,
    contract: dict,
    binding: dict,
    value: int,
    output: Path,
) -> tuple[State, dict, dict, list[dict]]:
    parent_events = _events(parent.transcript_path)
    child_end, = [
        row
        for row in parent_events
        if row.get("event") == "subagent_call_end"
        and row.get("child_node_type") == "experiment"
    ]
    child_root = parent.root.parent / child_end["child_run_id"]
    summary = json.loads((child_root / "summary.json").read_text(encoding="utf-8"))
    assert child_end["child_status"] == summary["status"] == "completed", summary

    run_start = next(
        row
        for row in _events(child_root / "transcript.jsonl")
        if row.get("event") == "run_start"
    )
    expected_task_contract = {
        "task_instance_uuid": task_instance_uuid,
        "digest": contract["task_contract_digest"],
        "revision": contract["task_contract_revision"],
    }
    parent_start, = [
        row
        for row in parent_events
        if row.get("event") == "subagent_call_start"
        and row.get("dispatch_id") == run_start["parent_dispatch_id"]
    ]
    assert {
        "task_instance_uuid": parent_start["task_instance_uuid"],
        "digest": parent_start["task_contract_digest"],
        "revision": parent_start["task_contract_revision"],
    } == expected_task_contract
    child = State.reopen(
        node_type="experiment",
        base_dir=child_root.parent,
        run_id=child_end["child_run_id"],
        project_id=parent.project_id,
        session_id=parent.session_id,
        project_worktree=worktree,
        task_instance_uuid=task_instance_uuid,
        task_contract_revision=contract["task_contract_revision"],
        task_contract_digest=contract["task_contract_digest"],
        parent_dispatch_id=run_start["parent_dispatch_id"],
    )
    records = _assert_child_authority(
        child,
        task_instance_uuid=task_instance_uuid,
        revision=contract["task_contract_revision"],
        digest=contract["task_contract_digest"],
        binding=binding,
        expected_action_count=1,
    )
    assert output.read_text(encoding="utf-8").strip() == str(value * value)
    action, = [
        row
        for row in records
        if row.get("kind") == "action_started"
        and row.get("tool") == "safe_run_bash"
    ]
    action_id = action["action_id"]
    context = action["execution_context"]
    assert context["task_contract"] == expected_task_contract
    assert context["prereg_binding"] == binding
    assert context["execution_params"] == {
        "scientific_params": {"value": value},
        "runtime_params": {},
    }
    assert context["parameter_comparison"]["passed"] is True
    settled, = [
        row
        for row in records
        if row.get("kind") == "action_settled"
        and row.get("action_id") == action_id
    ]
    assert settled["state"] == "settled" and settled["exit_code"] == 0

    manifest = delivery_transaction.read_manifest(child)
    assert manifest is not None
    assert manifest["delivery"]["scope"] == "scientific"
    assert manifest["delivery"]["outcome"] == "success"
    assert manifest["delivery"]["formal_input"]["binding"] == binding
    finish_row, = [row for row in records if row.get("kind") == "finish"]
    assert finish_row["closure_id"] == manifest["closure_id"]
    assert finish_row["artifact_ids"] == {
        role: manifest["artifacts"][role]["id"]
        for role in ("raw_results", "clean_results", "experiment_log")
    }
    for role in ("raw_results", "clean_results", "experiment_log"):
        artifact = manifest["artifacts"][role]
        record = child.read_artifact(artifact["id"])
        assert record is not None
        assert record["produced_by_run_id"] == child.run_id
        assert record["metadata"]["frozen"] is True
        assert record["metadata"]["artifact_role"] == role
        assert record["version"] == artifact["frozen"]["version"]
        assert record["content_hash"] == artifact["frozen"]["content_hash"]

    raw = json.loads(manifest["artifacts"]["raw_results"]["content"])
    assert raw["run_authority"]["task_contract"] == expected_task_contract
    assert raw["run_authority"]["prereg"] == binding
    raw_action, = [
        row
        for row in raw["ledger_records"]
        if row.get("kind") == "action_started"
        and row.get("action_id") == action_id
    ]
    assert raw_action["execution_context"] == context

    clean = json.loads(manifest["artifacts"]["clean_results"]["content"])
    assert clean["evaluation"]["required_total"] == 1
    assert clean["evaluation"]["required_satisfied"] == 1
    required_check, = [
        check
        for check in clean["evaluation"]["checks"]
        if check.get("name") == "square_result"
    ]
    assert required_check["passed"] is True
    assert required_check["evidence"]["producers"] == [action_id]
    lineage, = clean["evaluation"]["execution_lineage"]
    assert lineage["action_id"] == action_id
    assert lineage["relationship"] == "content_producer"
    assert lineage["prereg_binding"] == binding
    assert lineage["parameter_comparison"] == context["parameter_comparison"]

    audit = contract_audit.audit_experiment_delivery(child)
    assert audit["applicable"] is True and audit["passed"] is True, audit
    assert audit["status"] == "sealed" and audit["outcome"] == "success"
    return child, summary, manifest, records


def _assert_parent_review_pause(
    parent: State,
    result: dict,
    *,
    child_run_id: str,
    manifest: dict,
) -> None:
    assert result["status"] == "pause", result
    assert isinstance(result.get("pause_event"), dict), result
    flow, = [
        entry
        for entry in (parent.hook_state.get("pending_post_node_flow") or [])
        if entry.get("producing_run_id") == child_run_id
    ]
    trio_ids = {
        manifest["artifacts"][role]["id"]
        for role in ("raw_results", "clean_results", "experiment_log")
    }
    assert trio_ids.issubset(set(flow["artifact_ids"]))
    failures = [
        row
        for row in _events(parent.transcript_path)
        if row.get("event") == "runtime_reviewer_dispatch_failed"
        and row.get("producing_run_id") == child_run_id
    ]
    assert len(failures) == 1, failures
    assert "callable_nodes" in failures[0]["error"]
    assert "_reviewer" in failures[0]["error"]


@pytest.mark.asyncio
async def test_parent_selected_contract_revisions_deliver_fresh_scientific_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _initial_contract(
        tmp_path,
        monkeypatch,
        project_id="parent-contract-scientific-dispatch",
        max_turns=4,
    )
    worktree = fixture["worktree"]
    task_instance_uuid = fixture["task_instance_uuid"]
    binding_v1 = fixture["binding"]
    contract_v1 = fixture["contract"]
    outputs = {
        7: worktree / "experiments/runtime/square_v1.txt",
        8: worktree / "experiments/runtime/square_v2.txt",
    }
    calls = {7: 0, 8: 0}

    async def _bounded_scientific_script(
        _client: LLMClient,
        messages,
        **_kwargs,
    ) -> LLMResponse:
        prompt = "\n".join(str(getattr(row, "content", "") or "") for row in messages)
        selected = [
            value
            for value, marker in ((7, "dispatch-square-v1"), (8, "dispatch-square-v2"))
            if marker in prompt
        ]
        assert len(selected) == 1, prompt
        value = selected[0]
        output = outputs[value]
        calls[value] += 1
        call_number = calls[value]
        suffix = "v1" if value == 7 else "v2"
        prereg_version = binding_v1["version"] if value == 7 else binding_v1["version"] + 1
        if call_number == 1:
            program = f"value = {value}; print(value * value)"
            return LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": f"call_square_{suffix}",
                        "type": "function",
                        "function": {
                            "name": "safe_run_bash",
                            "arguments": json.dumps(
                                {
                                    "cmd": (
                                        f"{shlex.quote(sys.executable)} -c "
                                        f"{shlex.quote(program)} > "
                                        f"{shlex.quote(output.name)}"
                                    ),
                                    "timeout": 20,
                                    "execution_params": {
                                        "scientific_params": {"value": value},
                                        "runtime_params": {},
                                    },
                                }
                            ),
                        },
                    }
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
        if call_number == 2:
            safe_result = next(
                json.loads(row.content)
                for row in reversed(messages)
                if getattr(row, "role", "") == "tool"
                and getattr(row, "name", "") == "safe_run_bash"
            )
            assert safe_result["status"] == "success", safe_result
            return LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": f"call_finish_{suffix}",
                        "type": "function",
                        "function": {
                            "name": "finish",
                            "arguments": json.dumps(
                                {
                                    "outcome": "success",
                                    "summary": (
                                        f"Computed parent-selected v{prereg_version} square."
                                    ),
                                    "deliverables": [
                                        {
                                            "path": str(output),
                                            "obligation_id": "square_result",
                                        }
                                    ],
                                }
                            ),
                        },
                    }
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
        if call_number == 3:
            finish_result = next(
                json.loads(row.content)
                for row in reversed(messages)
                if getattr(row, "role", "") == "tool"
                and getattr(row, "name", "") == "finish"
            )
            assert finish_result["status"] == "success", finish_result
            return LLMResponse(
                content="The parent-selected scientific delivery is sealed.",
                tool_calls=[],
                finish_reason="stop",
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
        raise AssertionError("scientific child exceeded its three-call script")

    monkeypatch.setattr(LLMClient, "chat", _bounded_scientific_script, raising=True)
    first = await execute_tool(
        "run_node",
        fixture["parent"],
        node_type="experiment",
        user_note="派发父合同第一版，真实计算并冻结科学交付。",
        node_inputs={
            "experiment_spec": "dispatch-square-v1: compute the selected value squared.",
            "required_outputs": [
                {
                    "id": "square_result",
                    "kind": "file",
                    "path": str(outputs[7]),
                }
            ],
        },
        task_instance_uuid=task_instance_uuid,
        task_contract_digest=contract_v1["task_contract_digest"],
        resume_run_id="fresh",
    )

    child_v1, _, manifest_v1, records_v1 = _completed_scientific_child(
        fixture["parent"],
        worktree=worktree,
        task_instance_uuid=task_instance_uuid,
        contract=contract_v1,
        binding=binding_v1,
        value=7,
        output=outputs[7],
    )
    # The Experiment delivery itself is successful.  The real parent post-node
    # flow cannot dispatch its Reviewer in this fixture, so run_node honestly
    # pauses rather than treating that outer boundary as Experiment failure.
    _assert_parent_review_pause(
        fixture["parent"],
        first,
        child_run_id=child_v1.run_id,
        manifest=manifest_v1,
    )
    manifest_v1_snapshot = json.loads(json.dumps(manifest_v1))
    flow_v1_snapshot = json.loads(
        json.dumps(fixture["parent"].hook_state["pending_post_node_flow"])
    )
    output_v1_bytes = outputs[7].read_bytes()
    output_v1_stat = outputs[7].stat()
    output_v1_identity = (
        output_v1_stat.st_dev,
        output_v1_stat.st_ino,
        output_v1_stat.st_size,
        output_v1_stat.st_mtime_ns,
        output_v1_stat.st_ctime_ns,
    )

    # The first parent's real pending review remains untouched.  A second
    # authorized parent session selects the amended task contract and asks for
    # a genuinely fresh child instead of mutating or resuming the old run.
    parent_v2 = State.new(
        node_type="_orchestrator",
        base_dir=tmp_path / "runs",
        project_id=fixture["parent"].project_id,
        session_id=f"{fixture['session_id']}-v2",
        project_worktree=worktree,
    )
    parent_v2.hook_state["_callable_nodes"] = ["experiment"]
    producer_v2 = State.new(
        node_type="hypothesis",
        base_dir=tmp_path / "producer-runs",
        project_id=parent_v2.project_id,
        session_id="hypothesis-producer-v2-scientific",
        project_worktree=worktree,
    )
    binding_v2 = _frozen_prereg(
        producer_v2,
        value=8,
        amendment_reason="parent selected a corrected preregistration",
    )
    assert binding_v2["artifact_id"] == binding_v1["artifact_id"]
    assert binding_v2["version"] == binding_v1["version"] + 1
    assert binding_v2["content_hash"] != binding_v1["content_hash"]
    contract_v2 = await execute_tool(
        "task",
        parent_v2,
        action="contract",
        task_id=fixture["task_id"],
        objective="Use preregistration version two",
        intended_use="dispatch integration evidence",
        target_node="experiment",
        prereg_artifact_id=binding_v2["artifact_id"],
        prereg_version=str(binding_v2["version"]),
        prereg_content_hash=binding_v2["content_hash"],
        parent_revision_digest=contract_v1["task_contract_digest"],
        reason="fresh dispatch selects the corrected version",
    )
    assert contract_v2["status"] == "success", contract_v2
    assert contract_v2["task_contract_revision"] == (
        contract_v1["task_contract_revision"] + 1
    )

    second = await execute_tool(
        "run_node",
        parent_v2,
        node_type="experiment",
        user_note="派发父合同第二版，fresh child 真实计算并冻结科学交付。",
        node_inputs={
            "experiment_spec": "dispatch-square-v2: compute the selected value squared.",
            "required_outputs": [
                {
                    "id": "square_result",
                    "kind": "file",
                    "path": str(outputs[8]),
                }
            ],
            # A mutable stale hint cannot override Core's selected v2 contract.
            "prereg_assignment": {"kind": "bound", **binding_v1},
        },
        task_instance_uuid=task_instance_uuid,
        task_contract_digest=contract_v2["task_contract_digest"],
        resume_run_id="fresh",
    )
    child_v2, _, manifest_v2, _ = _completed_scientific_child(
        parent_v2,
        worktree=worktree,
        task_instance_uuid=task_instance_uuid,
        contract=contract_v2,
        binding=binding_v2,
        value=8,
        output=outputs[8],
    )
    _assert_parent_review_pause(
        parent_v2,
        second,
        child_run_id=child_v2.run_id,
        manifest=manifest_v2,
    )

    assert child_v2.run_id != child_v1.run_id
    assert calls == {7: 3, 8: 3}
    assert output_v1_bytes.strip() == b"49"
    assert outputs[7].read_bytes() == output_v1_bytes
    output_v1_stat_after = outputs[7].stat()
    assert (
        output_v1_stat_after.st_dev,
        output_v1_stat_after.st_ino,
        output_v1_stat_after.st_size,
        output_v1_stat_after.st_mtime_ns,
        output_v1_stat_after.st_ctime_ns,
    ) == output_v1_identity
    assert outputs[8].read_text(encoding="utf-8").strip() == "64"
    assert ledger.open_ledger(child_v1).records() == records_v1
    assert delivery_transaction.read_manifest(child_v1) == manifest_v1_snapshot
    assert (
        fixture["parent"].hook_state["pending_post_node_flow"]
        == flow_v1_snapshot
    )
    trio_v1 = {
        manifest_v1["artifacts"][role]["id"]
        for role in ("raw_results", "clean_results", "experiment_log")
    }
    trio_v2 = {
        manifest_v2["artifacts"][role]["id"]
        for role in ("raw_results", "clean_results", "experiment_log")
    }
    assert trio_v1.isdisjoint(trio_v2)


@pytest.mark.asyncio
async def test_parent_contract_revision_requires_a_fresh_exact_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _initial_contract(
        tmp_path,
        monkeypatch,
        project_id="parent-contract-dispatch",
    )
    worktree = fixture["worktree"]
    session_id = fixture["session_id"]
    parent = fixture["parent"]
    task_id = fixture["task_id"]
    task_instance_uuid = fixture["task_instance_uuid"]
    binding_v1 = fixture["binding"]
    contract_v1 = fixture["contract"]

    llm_calls: list[int] = []

    async def _bounded_stop(
        _client: LLMClient,
        _messages,
        **_kwargs,
    ) -> LLMResponse:
        call_number = len(llm_calls) + 1
        llm_calls.append(call_number)
        if call_number > 4:
            raise AssertionError("the two fresh children exceeded their bounded exits")
        if call_number % 2:
            return LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": f"call_fixture_blocker_{call_number}",
                        "type": "function",
                        "function": {
                            "name": "report_blocker",
                            "arguments": json.dumps(
                                {
                                    "summary": (
                                        "Integration fixture intentionally stops before "
                                        "executing an Experiment action."
                                    ),
                                    "category": "other",
                                    "suggested_owner": "experiment",
                                    "retryable_after_change": False,
                                }
                            ),
                        },
                    }
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
        return LLMResponse(
            content="The integration fixture is blocked without executing an action.",
            tool_calls=[],
            finish_reason="stop",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(LLMClient, "chat", _bounded_stop, raising=True)

    first = await execute_tool(
        "run_node",
        parent,
        node_type="experiment",
        user_note="先验证父任务合同第一版能精确到达 Experiment；本夹具不执行计算。",
        node_inputs={
            "experiment_spec": "Observe the exact parent-selected formal input.",
        },
        task_instance_uuid=task_instance_uuid,
        task_contract_digest=contract_v1["task_contract_digest"],
        resume_run_id="fresh",
    )
    child_v1, records_v1 = _assert_child_selected(
        first,
        task_instance_uuid=task_instance_uuid,
        revision=contract_v1["task_contract_revision"],
        digest=contract_v1["task_contract_digest"],
        binding=binding_v1,
        session_id=session_id,
    )

    producer_v2 = State.new(
        node_type="hypothesis",
        base_dir=tmp_path / "producer-runs",
        project_id=parent.project_id,
        session_id="hypothesis-producer-v2",
        project_worktree=worktree,
    )
    binding_v2 = _frozen_prereg(
        producer_v2,
        value=8,
        amendment_reason="parent selected a corrected preregistration",
    )
    assert binding_v2["artifact_id"] == binding_v1["artifact_id"]
    assert binding_v2["version"] == binding_v1["version"] + 1
    assert binding_v2["content_hash"] != binding_v1["content_hash"]

    contract_v2 = await execute_tool(
        "task",
        parent,
        action="contract",
        task_id=task_id,
        objective="Use preregistration version two",
        intended_use="dispatch integration evidence",
        target_node="experiment",
        prereg_artifact_id=binding_v2["artifact_id"],
        prereg_version=str(binding_v2["version"]),
        prereg_content_hash=binding_v2["content_hash"],
        parent_revision_digest=contract_v1["task_contract_digest"],
        reason="fresh dispatch selects the corrected version",
    )
    assert contract_v2["status"] == "success", contract_v2
    assert contract_v2["task_contract_revision"] == (
        contract_v1["task_contract_revision"] + 1
    )

    second = await execute_tool(
        "run_node",
        parent,
        node_type="experiment",
        user_note="再以 fresh 派发验证父任务合同第二版；旧 run 不应被改写或重做。",
        node_inputs={
            "experiment_spec": "Observe the newly selected formal input.",
            # A mutable stale hint is only a witness once Core selected v2.
            "prereg_assignment": {"kind": "bound", **binding_v1},
        },
        task_instance_uuid=task_instance_uuid,
        task_contract_digest=contract_v2["task_contract_digest"],
        resume_run_id="fresh",
    )
    child_v2, records_v2 = _assert_child_selected(
        second,
        task_instance_uuid=task_instance_uuid,
        revision=contract_v2["task_contract_revision"],
        digest=contract_v2["task_contract_digest"],
        binding=binding_v2,
        session_id=session_id,
    )

    assert child_v2.run_id != child_v1.run_id
    assert ledger.open_ledger(child_v1).records() == records_v1
    assert run_bootstrap.project_run_authority(records_v1)["prereg"] == binding_v1
    assert not [row for row in records_v2 if row.get("kind") == "action_started"]
    assert llm_calls == [1, 2, 3, 4]

    parent_events = _events(parent.transcript_path)
    starts = [row for row in parent_events if row.get("event") == "subagent_call_start"]
    for result in (first, second):
        child_start = next(
            row
            for row in _events(Path(result["child_state_dir"]) / "transcript.jsonl")
            if row.get("event") == "run_start"
        )
        assert any(
            row.get("dispatch_id") == child_start["parent_dispatch_id"]
            and row.get("task_contract_digest") == child_start["task_contract_digest"]
            for row in starts
        )


@pytest.mark.xfail(
    strict=True,
    raises=KnownManifestOnlyCompletionGap,
    reason=(
        "a text-only Experiment run currently lets its framework run_manifest "
        "count as delivery and advances the post-producing flow"
    ),
)
@pytest.mark.asyncio
async def test_text_only_manifest_cannot_complete_or_advance_downstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _initial_contract(
        tmp_path,
        monkeypatch,
        project_id="manifest-only-completion-gap",
    )
    parent = fixture["parent"]
    contract = fixture["contract"]
    task_instance_uuid = fixture["task_instance_uuid"]
    binding = fixture["binding"]

    llm_calls = 0

    async def _text_only_stop(
        _client: LLMClient,
        _messages,
        **_kwargs,
    ) -> LLMResponse:
        nonlocal llm_calls
        llm_calls += 1
        if llm_calls > 1:
            raise AssertionError("text-only child unexpectedly requested another LLM call")
        return LLMResponse(
            content="This text does not execute or deliver an experiment.",
            tool_calls=[],
            finish_reason="stop",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(LLMClient, "chat", _text_only_stop, raising=True)
    result = await execute_tool(
        "run_node",
        parent,
        node_type="experiment",
        user_note="验证只有文字、没有动作与三件套时不能完成或推动下游。",
        node_inputs={
            "experiment_spec": "Do not execute; this is a completion-boundary probe.",
        },
        task_instance_uuid=task_instance_uuid,
        task_contract_digest=contract["task_contract_digest"],
        resume_run_id="fresh",
    )
    assert llm_calls == 1

    parent_events = _events(parent.transcript_path)
    child_ends = [
        row
        for row in parent_events
        if row.get("event") == "subagent_call_end"
        and row.get("child_node_type") == "experiment"
    ]
    assert len(child_ends) == 1, child_ends
    child_end = child_ends[0]
    child_root = parent.root.parent / child_end["child_run_id"]
    summary = json.loads((child_root / "summary.json").read_text(encoding="utf-8"))
    child = SimpleNamespace(
        root=child_root,
        run_id=child_end["child_run_id"],
        session_id=fixture["session_id"],
    )
    _assert_child_authority(
        child,
        task_instance_uuid=task_instance_uuid,
        revision=contract["task_contract_revision"],
        digest=contract["task_contract_digest"],
        binding=binding,
    )

    assert summary["tool_call_count"] == 0, summary
    produced_types = list(summary.get("produced_artifact_types") or [])
    closure_types = {"raw_results", "clean_results", "experiment_log"}
    assert not closure_types.intersection(produced_types), summary
    deferred = [
        row
        for row in parent_events
        if row.get("event") == "decision_deferred_to_analysis"
        and row.get("producing_run_id") == child_end["child_run_id"]
    ]

    # One acceptable outcome within this one-response defect probe is a
    # non-completed child with no downstream advance.  That yields XPASS.
    # This fixture does not accept additional model turns or a newly sealed
    # delivery, so it is not a repair-acceptance test for Core reentry/resume.
    # The owner must test those paths separately and then retire this marker.
    if summary["status"] != "completed" and not deferred:
        return

    # Only the exact observed defect is swallowed.  Identity failures, a real
    # closure trio, an unexpected model/tool call, or a different flow shape
    # remains an ordinary test failure.
    assert summary["status"] == child_end["child_status"] == "completed", summary
    assert produced_types == ["run_manifest"], summary
    assert summary.get("node_task_outcome") is None, summary
    assert child_end.get("pending_integration_registered") is True, child_end
    skipped = [
        row
        for row in parent_events
        if row.get("event") == "post_node_review_skipped_no_artifacts"
        and row.get("producing_run_id") == child_end["child_run_id"]
    ]
    assert len(skipped) == 1, skipped
    assert len(deferred) == 1, deferred
    assert result.get("decision_state") == "deferred_to_analysis", result
    raise KnownManifestOnlyCompletionGap(
        "run_manifest alone completed a zero-action Experiment run and advanced "
        "the post-producing decision flow"
    )
