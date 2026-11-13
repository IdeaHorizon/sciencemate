import asyncio
import hashlib
import json

import pytest

from core.side_effect_contract import declare_read_only
from core.state import State
from nodes.experiment.tools import finish, obligations, resource_manager, result_acceptance as policy, safe_bash


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def state(tmp_path):
    from core.project_workspace import _git
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
         "commit", "--allow-empty", "-qm", "Initial fixture")
    state = State.new("experiment", tmp_path / "runs", session_id="inspection", project_id="inspection",
                      project_worktree=project)
    state.hook_state["node_inputs"] = {"experiment_spec": "Check existing observations."}
    return state


def test_frozen_dispatch_mode_cannot_be_relaxed_on_resume(state):
    state.hook_state["node_inputs"]["acceptance_mode"] = policy.MODE
    obligations.freeze(state)
    state.hook_state["node_inputs"].clear()
    assert policy.restricted(state)
    assert run(resource_manager._submit_job(state, "python helper.py", scheduler="slurm", dry_run=False,
                                          acceptance_mode="normal"))["executed"] is False


def test_generic_source_protection_is_not_a_no_rerun_order(state):
    declare_read_only(state, quote="保留输入原件，在新的输出目录重新运行。")
    assert not policy.restricted(state)


def test_historical_contract_stops_all_launch_paths_and_allows_report(state, monkeypatch):
    declare_read_only(state, quote="只检查已有结果，不重新生成或排程，附件保持原样")
    monkeypatch.setattr(safe_bash, "spawn_and_wait", lambda *a, **k: pytest.fail("subprocess launched"))
    cmd = 'mkdir -p acceptance/out/legacy && python acceptance/legacy_probe.py records.csv verify_results.py'
    for result in [
        run(safe_bash._safe_run_bash(state, cmd, stage="diagnostic", acceptance_mode="normal")),
        run(safe_bash._safe_execute_python(state, "import runpy; runpy.run_path('verify_results.py')")),
        run(resource_manager._submit_job(state, "python verify_results.py", dry_run=False)),
        run(finish._default_check_runner(state, {"command": "python verify_results.py"})),
    ]:
        assert result["reason"] == policy.MODE and result["executed"] is False
    src = state.root / "observed.csv"
    src.write_bytes(b"kind,group,trial,value\ninput,A,,99\nresult,A,1,2.0\nresult,A,2,2.0\n"
                    b"result,B,1,1.4\nresult,B,2,1.4\nsummary,,,1.7\n")
    sha = hashlib.sha256(src.read_bytes()).digest()
    stats = run(policy.inspect_result_table(state, str(src), "value", "group", {"kind": "result"}, "trial"))
    assert {k: (v["mean"], v["ids"], v["duplicate_ids"]) for k, v in stats["groups"].items()} == {
        "A": (2.0, ["1","2"], False), "B": (1.4, ["1","2"], False)}
    assert run(policy.inspect_result_table(state, str(src), "value", where={"kind": "summary"}))["groups"][""]["mean"] == 1.7
    assert run(safe_bash._safe_write_file(state, str(src), "replacement"))["reason"] == policy.MODE
    from nodes.experiment.tools import run_bootstrap
    run(run_bootstrap.establish(state))
    result = run(safe_bash._safe_write_file(state, "inspection.json", json.dumps(stats["groups"])))
    assert result["status"] == "success", result
    from core.project_workspace import working_directory
    report = working_directory(state) / "inspection.json"
    assert json.loads(report.read_bytes())["B"]["mean"] == 1.4
    assert hashlib.sha256(src.read_bytes()).digest() == sha


@pytest.mark.parametrize("data", ["g,v\na,nan\n", "g,v\na\n", "g,v,v\na,2,2\n",
                                  "g,v\na,1\n" + "a,2\n"*2000])
def test_never_report_partial_or_malformed_aggregate(state, data):
    p = state.root / "input.csv"
    p.write_text(data)
    assert run(policy.inspect_result_table(state, str(p), "v", "g"))["status"] == "error"


def test_real_duplicate_ids_are_not_hidden(state):
    p = state.root / "input.csv"
    p.write_text("g,i,v\na,1,2\na,1,4\n")
    detail = run(policy.inspect_result_table(state, str(p), "v", "g", record_id="i"))["groups"]["a"]
    assert detail["duplicate_ids"] and detail["ids"] == ["1","1"] and detail["mean"] == 3


def test_explicit_new_execution_reaches_normal_runner(state, monkeypatch):
    from nodes.experiment.tools import run_bootstrap
    run(run_bootstrap.establish(state))
    launches = []
    async def spy(*a, **k):
        launches.append(a)
        return "completed", 0, b"new computation", b""
    monkeypatch.setattr(safe_bash, "spawn_and_wait", spy)
    result = run(safe_bash._safe_run_bash(state, "printf 'new computation'"))
    assert len(launches) == 1, result
