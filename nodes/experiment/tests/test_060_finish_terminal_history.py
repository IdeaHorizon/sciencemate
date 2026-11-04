"""Public finish policy with real ledger, observations, checks and triplet disk writes.

Managed scheduler facts are explicit fixtures, not claims about a physical job.
Only the check execution boundary is a finite native Python child adapter. The
candidate complete module and unchanged real delivery transaction remain active.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import types

import pytest


def complete(coroutine):
    try:coroutine.send(None)
    except StopIteration as stopped:return stopped.value
    coroutine.close()
    raise AssertionError("Unexpected async suspension outside the finite test check boundary")


def command(source):
    return shlex.join([sys.executable,"-B","-c",source])


@pytest.fixture(scope="module")
def actual_module(tmp_path_factory):
    repo=Path(os.environ.get("HF_FINISH_REPO",str(Path(__file__).absolute().parents[3])))
    selected=os.environ.get("HF_FINISH_SOURCE")
    sys.dont_write_bytecode=True
    os.environ["HARNESS_FRAMEWORK_HOME"]=str(tmp_path_factory.mktemp("framework-home"))
    sys.path.insert(0,str(repo))
    if not selected:
        from nodes.experiment.tools import finish
        return finish
    selected=Path(selected)
    for name,path in (("nodes",repo/"nodes"),("nodes.experiment",repo/"nodes/experiment"),
                      ("nodes.experiment.tools",repo/"nodes/experiment/tools")):
        package=types.ModuleType(name);package.__path__=[str(path)];package.__package__=name
        sys.modules[name]=package
    spec=importlib.util.spec_from_file_location("nodes.experiment.tools.finish",selected)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(actual_module,monkeypatch,tmp_path):
    from core.state import State
    from nodes.experiment.tools import action_boundary,ledger,run_bootstrap
    adapter=os.environ.get("HF_FINISH_PATH_ADAPTER")=="1"
    if adapter:
        owned=tmp_path.absolute();original=Path.resolve
        def fixture_resolve(path,strict=False):
            value=Path(os.path.abspath(path))
            if not value.is_relative_to(owned):return original(path,strict=strict)
            if strict and not value.exists():raise FileNotFoundError(str(value))
            for part in [value,*value.parents]:
                if not part.is_relative_to(owned):break
                if part.exists():assert not getattr(part.lstat(),"st_file_attributes",0)&stat.FILE_ATTRIBUTE_REPARSE_POINT
            return value
        monkeypatch.setattr(Path,"resolve",fixture_resolve)
    runs=[]
    async def real_check(state,ob):
        finished=subprocess.run(shlex.split(ob["command"]),cwd=ob.get("cwd") or state.root,
            capture_output=True,timeout=8)
        runs.append({"command":ob["command"],"returncode":finished.returncode})
        return {"exit_code":finished.returncode,"stdout_tail":finished.stdout.decode("utf-8",errors="replace"),
            "stderr_tail":finished.stderr.decode("utf-8",errors="replace"),"error":None,"executed":True,
            "execution_profile":{"test_adapter":"finite native Python; Core isolation not certified"}}
    monkeypatch.setattr(actual_module,"_check_runner",real_check)

    def create(*,required_job_success=False,produce=True):
        state=State.new("experiment",tmp_path/"runs",project_id="fixture-project",session_id="fixture-owner")
        target=state.root/"runtime/result.txt"
        events=ledger.ledger_dir(state)/"events.jsonl"
        verification_source=("from pathlib import Path; "
            f"assert Path({str(target)!r}).read_text(encoding='utf-8')=='49\\n'")
        if required_job_success:
            verification_source+=("; import json; "
                f"rows=[json.loads(l) for l in Path({str(events)!r}).read_text(encoding='utf-8').splitlines()]; "
                "fact=next(r for r in rows if r.get('kind')=='job_terminal' and r.get('nonce')=='required-stage'); "
                "assert fact['derived_state']=='COMPLETED', 'required managed stage did not complete'")
        state.hook_state["node_inputs"]={"experiment_spec":"Produce result49 and verify the required actual execution facts.",
            "scope":"operation","operation_category":"software_validation",
            "prereg_assignment":{"kind":"none","reason":"No governing preregistration for this fixture."},
            "required_outputs":[{"id":"result","kind":"file","path":str(target)},
                {"id":"acceptance","kind":"check","command":command(verification_source),"expect_exit":0}]}
        complete(run_bootstrap.establish(state))
        assert ledger.bootstrap_phase(state)=="ready"
        led=ledger.open_ledger(state,write=True)
        if produce:
            runtime=target.parent;runtime.mkdir(parents=True,exist_ok=True)
            producer=command("from pathlib import Path; Path('result.txt').write_text('49\\n',encoding='utf-8')")
            handle=action_boundary.begin(state,tool="safe_run_bash",effect_class=action_boundary.EXPERIMENT,
                command=producer,work_root=str(runtime),output_candidates=[target])
            assert not isinstance(handle,action_boundary.BoundaryRefusal),handle
            child=subprocess.run(shlex.split(producer),cwd=runtime,capture_output=True,timeout=8)
            assert child.returncode==0,child.stderr
            settled=action_boundary.settle(state,handle,outcome="settled",exit_code=child.returncode)
            assert settled["status"]=="recorded",settled
        return state,led,target,runs
    return create


def job(led,nonce="failed-preparation",state="FAILED",code=7):
    led.append("intent",nonce=nonce,scheduler="slurm",output_root="fixture-output")
    led.append("job_bound",nonce=nonce,external_id="fixture-"+nonce)
    led.append("job_terminal",nonce=nonce,derived_state=state,exit_code=code,
               raw={"state":state,"exit_code":code,"fixture_scheduler_fact":True})


@pytest.mark.parametrize("terminal,exit_code",[("FAILED",7),("CANCELLED",143),("TIMEOUT",124)])
def test_current_acceptance_can_close_with_terminal_history_without_rewriting_it(actual_module,prepared,terminal,exit_code):
    state,led,target,runs=prepared()
    job(led,state=terminal,code=exit_code)
    initial=[dict(r) for r in led.records()]
    target_before=(target.read_bytes(),target.stat().st_ino,target.stat().st_mtime_ns)
    request={"outcome":"success","summary":"Requested task completed; historical attempts remain recorded.",
             "deliverables":[{"path":str(target)}]}
    result=complete(actual_module._finish(state,**request))
    assert result["status"]=="success" and result["outcome"]=="success",result
    assert result["required_satisfied"]=="2/2" and not result["gaps"]
    assert (target.read_bytes(),target.stat().st_ino,target.stat().st_mtime_ns)==target_before
    assert led.records()[:len(initial)]==initial
    raw=json.loads(state.read_artifact(result["artifact_ids"]["raw_results"])["content"])
    fact=next(r for r in raw["ledger_records"] if r.get("kind")=="job_terminal")
    assert fact["derived_state"]==terminal and fact["exit_code"]==exit_code
    assert any("non-success job terminals" in line for line in result["delivery"]["limitations"])
    assert result["delivery"]["external_job_refs"][0]["terminal"]["derived_state"]==terminal
    repeated=complete(actual_module._finish(state,**request))
    assert repeated["closure_id"]==result["closure_id"]
    assert repeated["artifact_ids"]==result["artifact_ids"]
    assert len([r for r in led.records() if r.get("kind")=="finish"])==1
    assert len([r for r in led.records() if r.get("kind")=="intent"])==1


def test_unrelated_later_success_does_not_override_failed_required_stage(actual_module,prepared):
    state,led,target,runs=prepared(required_job_success=True)
    job(led,nonce="required-stage",state="FAILED",code=7)
    job(led,nonce="unrelated-later-success",state="COMPLETED",code=0)
    result=complete(actual_module._finish(state,outcome="success",summary="A report exists and another job succeeded.",
        deliverables=[{"path":str(target)}]))
    assert result["status"]=="success_unsupported",result
    gap=next(g for g in result["gaps"] if g.get("obligation")=="acceptance")
    assert gap["gap"]=="check_failed"
    assert "required managed stage did not complete" in gap["evidence"]["stderr_tail"]
    assert runs[-1]["returncode"]==1
    assert not [r for r in led.records() if r.get("kind")=="finish"]


def test_terminal_history_and_zero_open_jobs_are_not_positive_task_evidence(actual_module,prepared):
    state,led,target,runs=prepared(produce=False)
    job(led)
    result=complete(actual_module._finish(state,outcome="success",summary="All jobs are terminal."))
    assert result["status"]=="success_unsupported",result
    assert any(g.get("obligation")=="result" for g in result["gaps"])
    assert not [r for r in led.records() if r.get("kind")=="finish"]


@pytest.mark.parametrize("phase",["intent","submitted","unknown"])
@pytest.mark.parametrize("outcome",["success","partial","failed","blocked","cancelled"])
def test_live_or_unknown_ownership_still_blocks_every_terminal_outcome(actual_module,prepared,phase,outcome):
    state,led,target,runs=prepared()
    led.append("intent",nonce="open-work",scheduler="slurm",output_root="fixture-output")
    if phase=="submitted":led.append("job_bound",nonce="open-work",external_id="fixture-open")
    if phase=="unknown":led.append("job_unknown",nonce="open-work",reason="lost acknowledgement")
    result=complete(actual_module._finish(state,outcome=outcome,summary="Attempt closure.",
        **({"blocker":{"summary":"Need authoritative settlement."}} if outcome=="blocked" else {})))
    assert result["status"]=="live_external_effect",result
    assert result["error_code"]=="terminal_closure_with_live_or_unknown_effect"
    assert result["live_jobs"][0]["state"]==phase
    assert not [r for r in led.records() if r.get("kind")=="finish"]


@pytest.mark.parametrize("outcome",["partial","failed","blocked","cancelled"])
def test_honest_non_success_closure_remains_available_with_unmet_requirements(actual_module,prepared,outcome):
    state,led,target,runs=prepared(produce=False)
    job(led)
    result=complete(actual_module._finish(state,outcome=outcome,summary="Task remains incomplete; all side effects ended.",
        **({"blocker":{"summary":"Required work unavailable."}} if outcome=="blocked" else {})))
    assert result["status"]=="success" and result["outcome"]==outcome,result
    assert result["gaps"]
    raw=json.loads(state.read_artifact(result["artifact_ids"]["raw_results"])["content"])
    assert any(r.get("kind")=="job_terminal" and r.get("derived_state")=="FAILED" for r in raw["ledger_records"])


def assert_unsealed(state, led):
    from nodes.experiment.tools import delivery_transaction
    assert delivery_transaction.read_manifest(state) is None
    assert not [row for row in led.records() if row.get("kind") == "finish"]


def test_failed_history_does_not_hide_current_required_producer_fatal(actual_module, prepared):
    state, led, target, _ = prepared()
    job(led)
    producer = next(row["action_id"] for row in led.records()
                    if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash")
    # This is an explicit fixture fact, just like the managed terminal rows.
    # The test exercises applicability to a real observed required output.
    led.append("action_settled", action_id=producer, outcome="failed", exit_code=7,
               fatal_signals=["FATAL: current required result is invalid"])
    result = complete(actual_module._finish(state, outcome="success", summary="File check passed.",
                      deliverables=[{"path": str(target)}]))
    assert result["status"] == "success_unsupported", result
    assert any(row.get("name") == "fatal_signal" and row.get("action") == producer
               for row in result["contradictions"]), result
    assert_unsealed(state, led)


def test_failed_history_does_not_bypass_current_output_identity(actual_module, prepared):
    state, led, target, _ = prepared()
    job(led)
    before = target.stat()
    # Same bytes still pass the content check, but a later unobserved mutation
    # must not become the captured producer's immutable output identity.
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    result = complete(actual_module._finish(state, outcome="success", summary="Content is still 49.",
                      deliverables=[{"path": str(target)}]))
    assert result["status"] == "success_unsupported", result
    assert any(gap.get("gap") == "identity_changed_since_the_action" for gap in result["gaps"]), result
    assert_unsealed(state, led)


@pytest.mark.parametrize("outcome", ["success", "partial", "failed", "blocked", "cancelled"])
def test_failed_terminal_history_does_not_allow_abandoning_open_action(actual_module, prepared, outcome):
    state, led, target, _ = prepared()
    job(led)
    led.append("action_started", action_id="still-running-action", tool="safe_run_bash",
               effect_class="experiment", command="fixture-only no process launched")
    result = complete(actual_module._finish(state, outcome=outcome, summary="Try honest closure.",
                      **({"blocker": {"summary": "Open action needs settlement."}} if outcome == "blocked" else {})))
    assert result["status"] == "live_external_effect", result
    assert any(row.get("action_id") == "still-running-action" for row in result["open_actions"]), result
    assert_unsealed(state, led)
