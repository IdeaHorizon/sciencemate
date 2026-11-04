"""Target-bound Slurm lifecycle; no real SSH connections or cluster jobs."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from core.state import State
from nodes.experiment.tools import ledger, resource_manager as rm, run_bootstrap, slurm_ssh


TARGET = {"host": "cluster-login", "user": "researcher", "login_shell": True}
PROBE = {"ok": True, "user": "researcher", "cluster": "research", "tools": {
    name: "/opt/slurm/bin/" + name for name in ("sinfo", "sbatch", "squeue", "sacct", "scancel", "scontrol")}}


def _state(tmp_path):
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="ssh-lifecycle", session_id="owner")
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Run a bounded CPU job on the selected Slurm target and report its output.",
        "prereg_assignment": {"kind": "none", "reason": "operational integration test"}}
    asyncio.run(run_bootstrap.establish(state))
    return state


def _record(**values):
    return {"scheduler": "slurm", "job_id": "731", "slurm_target": TARGET,
            "launch_host": "cluster-login", "scheduler_cluster": "research", "remote_user": "researcher",
            "submission_nonce": "nonce-one", "workdir": "/scratch/a project", "output_roots": ["/scratch/a project"],
            "scheduler_output_dir": "/scratch/a project", "health_contract": {}, **values}


def _reply(stdout="", *, ok=True):
    return {"ok": ok, "returncode": 0 if ok else 1, "stdout": stdout, "stderr": ""}


def _transport(monkeypatch, operation):
    calls = []
    def run(target, argv, timeout=20, **kwargs):
        calls.append((dict(target), list(argv)))
        if argv == ["id", "-un"]:
            return _reply(target.get("user", "researcher"))
        if argv == ["scontrol", "show", "config"]:
            return _reply("ClusterName = research\n")
        return operation(target, argv)
    monkeypatch.setattr(slurm_ssh, "run", run)
    monkeypatch.setattr(rm, "_run", lambda *_a, **_k: pytest.fail("remote operation reached submit-host CLI"))
    return calls


def _submission(tmp_path, monkeypatch, *, submit_reply=None, prepare_ok=True):
    state = _state(tmp_path)
    preparation = []
    def prepare(state_arg, target, **kwargs):
        assert state_arg is state
        intents = state.list_artifacts("external_submission_intent")
        assert len(intents) == 1, "upload must follow durable intent"
        payload = json.loads(state.read_artifact(intents[0]["id"])["content"])
        assert payload["slurm_target"]["host"] == "cluster-login"
        preparation.append(kwargs)
        return {"ok": prepare_ok, "bootstrap_log_dir": "/scratch/a project/.bootstrap", "remote_script_path": "/scratch/a project/.bootstrap/job.sh", "error": None if prepare_ok else "upload disconnected"}
    files = SimpleNamespace(
        bootstrap_path=lambda nonce, name, output: output + "/.bootstrap",
        remote_stage_in=lambda entries, bootstrap: [{**e, "src": bootstrap + "/inputs/" + str(i)} for i,e in enumerate(entries)],
        prepare_submission=prepare)
    monkeypatch.setattr(rm, "_slurm_files", lambda: files)
    from nodes.experiment.tools import job_settlement
    monkeypatch.setattr(job_settlement, "capture_submission_outputs", lambda *_a: {"roots": [], "candidates": [], "snapshot": {"entries": {}, "truncated": False}})
    def operation(target, argv):
        if argv[0] == "scontrol":
            return _reply("JobId=731 UserId=researcher(1001) Comment=ai4s:nonce-one SubmitTime=2026-10-22T12:00:00")
        assert argv[0] == "sbatch", argv
        kinds = [r["kind"] for r in ledger.open_ledger(state).records()]
        assert "intent" in kinds and "submission_attempted" in kinds
        assert kinds.index("intent") < kinds.index("submission_attempted")
        return submit_reply or _reply("731;research\n")
    calls = _transport(monkeypatch, operation)
    result = rm._submit_sync(
        runtime_root=tmp_path / "submit-runtime", scheduler="slurm", command="printf 'measured\\n' > result.txt",
        job_name="cpu_job", mpi_ranks=1, cpus_per_rank=1, gpus=0, memory_gb=1.0, storage_gb=1.0,
        walltime_minutes=5, queue=None, nodelist=None, image=None,
        workdir="/scratch/a project", dry_run=False, namespace=None, output_dir="/scratch/a project",
        output_paths=["/scratch/a project"], stage_in=[], state=state, submission_nonce="nonce-one",
        slurm_target=TARGET, slurm_probe=PROBE)
    return state, result, calls, preparation


def test_ssh_submit_uploads_after_intent_and_only_uses_posix_scheduler_paths(tmp_path, monkeypatch):
    state, result, calls, preparation = _submission(tmp_path, monkeypatch)
    assert result["status"] == "success", result
    assert result["job_id"] == "731"
    assert result["launch_host"] == TARGET["host"]
    assert result["remote_user"] == "researcher"
    assert result["scheduler_cluster"] == "research"
    assert result["remote_write_isolation"] == "missing"
    submits = [argv for _, argv in calls if argv[0] == "sbatch"]
    assert len(submits) == 1
    assert submits[0][-1] == "/scratch/a project/.bootstrap/job.sh"
    assert "--chdir=/scratch/a project" in submits[0]
    assert all("\\" not in p for p in submits[0])
    assert Path(result["script_path"]).is_file()
    assert b"\r\n" not in Path(result["script_path"]).read_bytes()
    assert result["scheduler_submit_time"] == "2026-10-22T12:00:00"
    assert "--clusters=research" in submits[0]
    assert "#SBATCH --comment=ai4s:nonce-one" in Path(result["script_path"]).read_text(encoding="utf-8")
    assert len(preparation) == 1
    assert ledger.project_jobs(ledger.open_ledger(state).records())["nonce-one"]["external_id"] == "731"


def test_upload_failure_never_crosses_sbatch_boundary(tmp_path, monkeypatch):
    state, result, calls, preparation = _submission(tmp_path, monkeypatch, prepare_ok=False)
    assert result["reason"] == "slurm_staging_failed", result
    assert result["job_submitted"] is False
    assert not any(argv[0] == "sbatch" for _,argv in calls)
    assert not any(r["kind"] == "submission_attempted" for r in ledger.open_ledger(state).records())


def test_ssh_disconnect_after_submission_marker_stays_unknown(tmp_path, monkeypatch):
    reply = {"ok": False, "returncode": 255, "stdout": "", "stderr": "connection lost", "transport_error": "ssh_failed"}
    state, result, calls, _ = _submission(tmp_path, monkeypatch, submit_reply=reply)
    assert result["status"] == "submission_outcome_unknown", result
    assert result["do_not_resubmit"] is True
    assert len([argv for _,argv in calls if argv[0] == "sbatch"]) == 1
    assert ledger.project_jobs(ledger.open_ledger(state).records())["nonce-one"]["state"] == "unknown"


def test_remote_health_contract_does_not_apply_windows_drive_or_home():
    contract = rm._health_contract({"completion_paths": ["results/out.dat"], "progress_paths": ["logs/progress"]},
                                  workdir="/scratch/a project", output_roots=["/scratch/a project"],
                                  scheduler_output_dir=PurePosixPath("/scratch/a project/logs"), expected_duration_s=None, remote=True)
    assert contract["completion_paths"] == ["/scratch/a project/results/out.dat"]
    with pytest.raises(ValueError):
        rm._health_contract({"completion_paths": ["../outside"]}, workdir="/scratch/a project",
                            output_roots=["/scratch/a project"], scheduler_output_dir=PurePosixPath("/scratch/a project/logs"),
                            expected_duration_s=None, remote=True)


def test_remote_staging_script_preserves_posix_paths_with_spaces():
    lines = rm._stage_in_lines("/scratch/a project", [{"src": "/scratch/staged/in", "dst": "inputs/data file", "mode": "0644"}], remote=True)
    import shlex
    argv = shlex.split(lines[0])
    assert argv[:2] == ["python3", "-c"]
    request = json.loads(argv[3])
    assert request["workdir"] == "/scratch/a project"
    assert request["files"][0]["dst"] == "inputs/data file"
    assert "O_NOFOLLOW" in argv[2] and "dir_fd=parent" in argv[2]
    compile(argv[2], "remote stage", "exec")


@pytest.mark.parametrize("path", ["/", "/etc/app", "/usr/local", "/home/user/.ssh/keys", "/work/.git/config"])
def test_fixed_remote_write_protection_is_independent_of_path_role_registration(path):
    result = rm._ssh_submission_path_guard(path, path, [path])
    assert result["reason"] == "protected_remote_path"


def test_remote_personal_directory_does_not_require_environment_registration():
    assert rm._ssh_submission_path_guard("/home/user/my tools", "/home/user/results", ["/home/user/results"]) is None


def test_status_uses_the_persisted_target_and_nonce_selects_duplicate_job_ids(monkeypatch):
    first = _record()
    second = _record(slurm_target={**TARGET, "host": "other-login"}, launch_host="other-login", submission_nonce="nonce-two")
    monkeypatch.setattr(rm, "_submission_payloads", lambda _s: [first, second])
    monkeypatch.setattr(rm, "_task_external_jobs", lambda _s: [])
    calls = _transport(monkeypatch, lambda target, argv: _reply("731|RUNNING|00:01|node|researcher|ai4s:nonce-two"))
    state = SimpleNamespace()
    ambiguous = asyncio.run(rm._job_status(state, "slurm", "731"))
    assert ambiguous["status"] == "error"
    assert calls == []
    result = asyncio.run(rm._job_status(state, "slurm", "731", submission_nonce="nonce-two"))
    assert result["status"] == "success", result
    assert all(target["host"] == "other-login" for target,_ in calls)


def test_changed_cluster_never_reaches_scheduler_query_or_cancel(monkeypatch):
    calls=[]
    def run(target,argv,**kwargs):
        calls.append((argv,kwargs))
        assert kwargs["expected_identity"] == {"user":"researcher", "cluster":"research"}
        assert "--clusters=research" in argv
        return {"ok":False,"returncode":125,"stdout":"","stderr":"identity changed",
                "transport_error":"target_identity_mismatch","remote_effects":"not_sent"}
    monkeypatch.setattr(slurm_ssh,"run",run)
    result=rm._job_status_sync("slurm","731",None,slurm_target=TARGET,expected_cluster="research",submission_nonce="nonce-one")
    assert result["status"]=="error"
    assert result["raw"]["transport_error"]=="target_identity_mismatch"
    assert len(calls)==1


@pytest.mark.parametrize("terminal", [False, True])
def test_scancel_ack_is_not_physical_cancellation(monkeypatch, terminal):
    calls = _transport(monkeypatch, lambda target, argv:
                       _reply("731|researcher|ai4s:nonce-one" if argv[0]=="squeue" else ""))
    monkeypatch.setattr(rm,"_remote_job_ended_on_its_own",lambda *_a,**_k:
                        {"allocation_states":["CANCELLED"],"allocation_state":"CANCELLED"} if terminal else None)
    result=rm._cancel_sync("slurm","731",None,slurm_target=TARGET,expected_cluster="research",submission_nonce="nonce-one")
    assert result["ok"] is terminal
    assert result["outcome_unknown"] is (not terminal)
    assert len([argv for _,argv in calls if argv[0]=="scancel"])==1


def test_cancel_does_not_signal_another_job_with_the_same_numeric_id(monkeypatch):
    calls=_transport(monkeypatch,lambda target,argv:_reply("731|researcher|ai4s:someone-else"))
    monkeypatch.setattr(rm,"_remote_job_ended_on_its_own",lambda *_a,**_k:None)
    result=rm._cancel_sync("slurm","731",None,slurm_target=TARGET,expected_cluster="research",submission_nonce="nonce-one")
    assert result["reason"]=="slurm_cancellation_identity_unconfirmed"
    assert not any(argv[0]=="scancel" for _,argv in calls)


def test_health_reads_remote_files_and_accounting_on_the_same_target(monkeypatch):
    record=_record(health_contract={"completion_paths":["/scratch/a project/result.txt"],"progress_paths":[],"poll_interval_s":30,"stall_after_s":90})
    monkeypatch.setattr(rm,"_authoritative_record_for_read",lambda *_a,**_k:(record,None))
    _transport(monkeypatch,lambda target,argv:_reply("731|RUNNING|00:01|node|researcher|ai4s:nonce-one"))
    observed=[]
    files=SimpleNamespace(health_snapshot=lambda rec,path: observed.append((rec,path)) or {"path":path,"exists":True,"kind":"file","size_bytes":12},tail=lambda *_a,**_k:"")
    monkeypatch.setattr(rm,"_slurm_files",lambda:files)
    monkeypatch.setattr(rm,"_health_file_snapshot",lambda *_a:pytest.fail("remote output was read as a local path"))
    result=rm._probe_external_job_health_impl(SimpleNamespace(),"slurm","731",submission_nonce="nonce-one")
    assert result["status"]=="success",result
    assert result["completion_paths"][0]["path"]=="/scratch/a project/result.txt"
    assert all(rec is record for rec,path in observed)


def test_remote_terminal_hash_uses_remote_identity_not_submit_host(monkeypatch):
    state=SimpleNamespace(append_transcript=lambda *_a,**_k:None)
    record=_record()
    monkeypatch.setattr(rm,"_slurm_files",lambda:SimpleNamespace(output_identity=lambda rec,path:{"sha256":"a"*64}))
    monkeypatch.setattr(rm._output_postconditions,"output_identity",lambda *_a:pytest.fail("remote hash reached local filesystem"))
    result=rm.freeze_first_terminal_output_observation(state,record,{"status":"success","scheduler_phase":"terminal","completion_paths":[{"path":"/scratch/a project/result.txt","exists":True,"kind":"file"}]})
    assert result["completion_paths"][0]["sha256"]=="a"*64


def test_terminal_output_collection_can_be_retried_without_submit(monkeypatch):
    record=_record()
    monkeypatch.setattr(rm,"_external_job_record",lambda *_a,**_k:record)
    monkeypatch.setattr(rm,"_job_status_sync",lambda *_a,**_k:{"status":"success","raw":_reply("")})
    monkeypatch.setattr(rm,"_settle_if_terminal",lambda *_a,**_k:{"settled":True,"already":True})
    monkeypatch.setattr(rm,"_remote_job_ended_on_its_own",lambda *_a,**_k:{"allocation_states":["COMPLETED"]})
    collected=[]
    monkeypatch.setattr(rm,"_slurm_files",lambda:SimpleNamespace(collect_outputs=lambda state,rec,**kw:collected.append((rec,kw)) or {"status":"collected"}))
    result=asyncio.run(rm._job_status(SimpleNamespace(),"slurm","731",submission_nonce="nonce-one",collect_outputs=True,max_output_bytes=1024))
    assert result["output_collection"]["status"]=="collected"
    assert collected==[(record,{"max_bytes":1024})]


@pytest.mark.parametrize("canonical", ["/scratch/a project", "/scratch/canonical project"])
def test_public_submit_binds_target_and_hashes_to_existing_confirmation(tmp_path, monkeypatch,canonical):
    from shared.lib import dangerous_commands as danger
    state=_state(tmp_path)
    monkeypatch.setattr(slurm_ssh,"probe",lambda target:PROBE)
    monkeypatch.setattr(rm,"_slurm_files",lambda:SimpleNamespace(check_paths=lambda *_a,**_k:{"ok":True,"workdir":canonical,"output_dir":canonical}))
    monkeypatch.setattr(rm,"_submit_sync",lambda *_a,**_k:pytest.fail("unconfirmed call submitted a job"))
    monkeypatch.setattr(danger,"bypass_enabled",lambda:False)
    monkeypatch.setattr(danger,"is_confirmed",lambda *_a:False)
    monkeypatch.setattr(danger,"build_pause_payload",lambda state,**kwargs:{"status":"pause","approval_text":kwargs["text"]})
    result=asyncio.run(rm._submit_job(state,command="printf 'x\\n' > result.txt",scheduler="slurm",
                                    slurm_target={"host":"cluster-login"},workdir="/scratch/a project",
                                    walltime_minutes=5,dry_run=False))
    assert result["status"]=="pause",result
    approved=json.loads(result["approval_text"])
    assert approved["slurm_target"]["host"]=="cluster-login"
    assert approved["slurm_target"]["user"]=="researcher"
    assert approved["scheduler_cluster"]=="research"
    assert approved["workdir"]==canonical
    assert approved["output_dir"]==canonical


def test_output_reservations_do_not_mix_local_or_other_target_paths(monkeypatch):
    monkeypatch.setattr(rm,"_job_lifecycle_states",lambda *_a:{})
    monkeypatch.setattr(rm,"_submission_payloads",lambda *_a:[_record()])
    monkeypatch.setattr(rm,"_task_external_jobs",lambda *_a:[])
    monkeypatch.setattr(rm,"_unknown_orphan_records",lambda *_a:[])
    from nodes.experiment.tools import external_submission_recovery as recovery
    monkeypatch.setattr(recovery,"unresolved_submission_output_reservations",lambda *_a:[])
    monkeypatch.setattr(rm,"_external_job_is_active",lambda *_a,**_k:pytest.fail("unrelated target was queried"))
    assert rm._active_output_conflicts(SimpleNamespace(),["/scratch/a project"],slurm_target={**TARGET,"host":"other-login"})==[]
    assert rm._active_output_conflicts(SimpleNamespace(),["/scratch/a project"])==[]


def test_unknown_cancellation_reentry_queries_instead_of_sending_another_signal(monkeypatch):
    record=_record()
    intent={"intent_artifact_id":"intent","target_lifecycle":"cancelled","reason":"test","identity":{"scheduler":"slurm","job_id":"731"}}
    monkeypatch.setattr(rm,"_external_job_record",lambda *_a,**_k:record)
    monkeypatch.setattr(rm,"lifecycle_for_submission",lambda *_a,**_k:{"status":"running"})
    monkeypatch.setattr(rm,"_latest_cancellation_transaction",lambda *_a:{"intent":intent,"outcome":{"outcome":"unknown"}})
    async def ended(*args):
        return {"source":"slurm_accounting","allocation_states":["CANCELLED"],"allocation_state":"CANCELLED"}
    monkeypatch.setattr(rm,"_observed_job_end",ended)
    monkeypatch.setattr(rm,"_settle_if_terminal",lambda *_a,**_k:{"settled":True})
    monkeypatch.setattr(rm,"_persist_cancellation_outcome",lambda *_a,**_k:{"outcome":"confirmed"})
    monkeypatch.setattr(rm,"_close_confirmed_cancellation",lambda *_a,**kw:{"status":"success","idempotent":kw["idempotent"]})
    monkeypatch.setattr(rm,"_cancel_sync",lambda *_a,**_k:pytest.fail("reentry sent another cancellation"))
    result=asyncio.run(rm._cancel_job(SimpleNamespace(),"slurm","731",submission_nonce="nonce-one"))
    assert result=={"status":"success","idempotent":True}


@pytest.mark.parametrize("command", [
    "printf x > /home/user/.ssh/config", "cd /etc && printf x > hosts",
    "cp data /work/.research/ledger", "tee /home/user/.git/config < data",
    "dd if=data of=/etc/hosts", "gcc x.c -o /usr/local/bin/x",
])
def test_remote_command_literal_writes_cannot_bypass_protected_paths(command):
    result=rm._ssh_submission_path_guard("/scratch/work", "/scratch/work", [], command=command)
    assert result["reason"]=="protected_remote_command_path"


def test_remote_read_and_personal_install_do_not_need_role_registration():
    assert rm._ssh_submission_path_guard("/home/u/work", "/home/u/results", [],
                                         command="cat /etc/os-release; mkdir -p /home/u/tools; cp binary /home/u/tools/app") is None


@pytest.mark.parametrize("destination", [r"\etc\x", r"C:\etc\x", "/etc/x", "../x", "nested/../../x"])
def test_remote_stage_destination_is_posix_and_relative(tmp_path,monkeypatch,destination):
    source=tmp_path / "input.dat"; source.write_bytes(b"data")
    monkeypatch.setattr(rm,"_stage_in_readable",lambda *_a:True)
    with pytest.raises(ValueError):
        rm._validate_stage_in(SimpleNamespace(),[{"src":str(source),"dst":destination}],remote=True)


def _accounting_row(job="731", user="researcher", comment="ai4s:nonce-one", submit="2026-10-22T12:00:00", cluster="research", state="COMPLETED"):
    fields={"JobIDRaw":job,"State":state,"ExitCode":"0:0"}
    return "|".join(fields.get(key,"") for key in rm._SLURM_ACCOUNTING_FIELDS)+(f"|{user}|{comment}|{submit}|{cluster}")


@pytest.mark.parametrize("changes", [{"job":"732"},{"user":"other"},{"comment":"ai4s:other"},{"cluster":"other"},{"comment":""}])
def test_remote_accounting_does_not_settle_reused_or_unbound_identity(monkeypatch,changes):
    _transport(monkeypatch,lambda *_a:_reply(_accounting_row(**changes)))
    assert rm._remote_job_ended_on_its_own("slurm",{},"731",**rm._slurm_kwargs(_record())) is None


def test_remote_accounting_without_comment_can_use_frozen_scheduler_submit_time(monkeypatch):
    _transport(monkeypatch,lambda *_a:_reply(_accounting_row(comment="")))
    record=_record(scheduler_submit_time="2026-10-22T12:00:00")
    result=rm._remote_job_ended_on_its_own("slurm",{},"731",**rm._slurm_kwargs(record))
    assert result["allocation_state"]=="COMPLETED"
    record["scheduler_submit_time"]="2026-10-21T12:00:00"
    assert rm._remote_job_ended_on_its_own("slurm",{},"731",**rm._slurm_kwargs(record)) is None


def test_remote_accounting_only_reduces_matching_allocation(monkeypatch):
    output=_accounting_row()+"\n"+_accounting_row(job="732",state="FAILED")
    _transport(monkeypatch,lambda *_a:_reply(output))
    result=rm._remote_job_ended_on_its_own("slurm",{},"731",**rm._slurm_kwargs(_record()))
    assert result["allocation_states"]==["COMPLETED"]


def test_remote_status_rejects_other_nonce_before_health_uses_it(monkeypatch):
    _transport(monkeypatch,lambda *_a:_reply("731|RUNNING|00:01|node|researcher|ai4s:other"))
    result=rm._job_status_sync("slurm","731",None,**rm._slurm_kwargs(_record()))
    assert result["status"]=="error"
    assert result["raw"]["identity_unconfirmed"] is True


def test_ack_cluster_mismatch_retains_intent_and_does_not_guess_target(tmp_path,monkeypatch):
    state,result,calls,_=_submission(tmp_path,monkeypatch,submit_reply=_reply("731;unapproved"))
    assert result["reason"]=="slurm_ack_cluster_mismatch"
    assert result["scheduler_cluster"]=="research"
    assert result["acknowledged_identity"]["scheduler_cluster"]=="unapproved"
    assert result["do_not_resubmit"] is True
    assert not any(argv[0]=="scontrol" for _,argv in calls)


def test_handoff_status_uses_persisted_ssh_target_instead_of_submit_host(monkeypatch):
    import sys
    from nodes.experiment import hooks
    monkeypatch.setitem(sys.modules, "tools.resource_manager", rm)
    record = _record(scheduler_submit_time="2026-10-22T12:00:00")
    calls = []
    def status(scheduler, job_id, namespace, **kwargs):
        calls.append((scheduler, job_id, namespace, kwargs))
        return {"status": "success", "raw": _reply("731|RUNNING|00:01|node")}
    monkeypatch.setattr(rm, "_job_status_sync", status)
    assert hooks._classify_external_job_status(record) == "running"
    assert len(calls) == 1
    assert calls[0][3] == {"remote_host": "cluster-login", "container_runtime_id": None,
                            **rm._slurm_kwargs(record)}


def test_turn_reconciliation_passes_each_nonce_for_duplicate_scheduler_job_ids(monkeypatch):
    import sys
    from nodes.experiment import hooks
    from nodes.experiment.tools import external_submission_recovery as recovery
    monkeypatch.setitem(sys.modules, "tools.resource_manager", rm)
    monkeypatch.setitem(sys.modules, "tools.external_submission_recovery", recovery)
    monkeypatch.setattr(recovery, "dangling_external_submission_intents", lambda _s: [])
    monkeypatch.setattr(hooks, "_reconcile_ledger_invalid_blocker", lambda *_a: None)
    jobs = [_record(task_id="task-one"),
            _record(task_id="task-two", submission_nonce="nonce-two", launch_host="other-login",
                    slurm_target={**TARGET, "host": "other-login"})]
    monkeypatch.setattr(hooks, "_external_job_tasks_for_reconciliation", lambda _s: jobs)
    calls = []
    def health(state, scheduler, job_id, namespace, **kwargs):
        calls.append((scheduler, job_id, kwargs))
        return {"status": "success", "workflow_status": "awaiting_external_job", "health_state": "healthy"}
    monkeypatch.setattr(rm, "probe_external_job_health", health)
    state = SimpleNamespace(hook_state={}, append_transcript=lambda *_a, **_k: None)
    result = hooks.external_job_reconciliation_on_turn_start(SimpleNamespace(state=state))
    assert result
    assert calls == [("slurm", "731", {"submission_nonce": "nonce-one"}),
                     ("slurm", "731", {"submission_nonce": "nonce-two"})]