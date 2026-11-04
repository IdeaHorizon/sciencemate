"""Opt-in real CPU Slurm protocol acceptance; no model or scientific benchmark.

Uses the caller's existing SSH alias and trusted host key. Creates uniquely
named remote job directories, two bounded CPU jobs and local evidence. Run on
an account/partition authorized for this test. No credentials are accepted.
"""

import argparse
import asyncio
import hashlib
import json
import platform
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from core.state import State
from nodes.experiment.tools import (
    external_submission_recovery as recovery,
)
from nodes.experiment.tools import (
    ledger,
    run_bootstrap,
    slurm_ssh,
)
from nodes.experiment.tools import (
    resource_manager as rm,
)
from shared.lib import dangerous_commands


def require(ok, value):
    if not ok:
        raise AssertionError(value)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Existing personal OpenSSH alias")
    parser.add_argument(
        "--remote-base",
        required=True,
        help="Authorized existing POSIX directory shared with compute nodes",
    )
    parser.add_argument(
        "--output-dir", required=True, help="Local directory for isolated State and evidence"
    )
    args = parser.parse_args()
    root = Path(args.output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = {"host": args.host}
    remote_base = slurm_ssh.normalize_path(args.remote_base)
    evidence = {"client": platform.platform(), "python": sys.version, "steps": []}

    def record(name, value):
        evidence["steps"].append({"name": name, "value": value})
        (root / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf8"
        )
        print(name, value.get("status", value.get("ok", "recorded")), flush=True)

    probe = slurm_ssh.probe(target)
    record("probe", probe)
    require(probe.get("ok"), probe)
    dangerous_commands.set_preauthorized_categories({"真实外部作业提交", "取消真实外部作业"})
    state = State.new(
        node_type="experiment",
        base_dir=root / "runs",
        project_id="slurm-contract-acceptance",
        session_id="owner",
    )
    state.hook_state["node_inputs"] = {
        "scope": "operation",
        "operation_category": "scheduler_probe",
        "experiment_spec": "Validate the authorized isolated Slurm CPU job lifecycle and return the exact staged bytes.",
        "prereg_assignment": {"kind": "none", "reason": "operational acceptance"},
    }
    await run_bootstrap.establish(state)
    name = "job-" + uuid.uuid4().hex[:8]
    remote = remote_base + "/" + name
    setup = slurm_ssh.run(target, ["mkdir", "-p", remote])
    require(setup.get("ok"), setup)
    inp = state.root / "input.bin"
    inp.write_bytes(bytes(range(256)) * 8 + b"\x00\r\nexact")
    submit_args = dict(
        command="sleep 3; cp input.bin result.bin; printf 'verified\\n' > measured.txt",
        scheduler="slurm",
        slurm_target=target,
        job_name=name,
        workdir=remote,
        output_dir=remote,
        output_paths=[remote],
        stage_in=[{"src": str(inp), "dst": "input.bin"}],
        memory_gb=0.0625,
        storage_gb=0.01,
        walltime_minutes=1,
        dry_run=False,
    )
    result = await rm._submit_job(state, **submit_args)
    if result.get("status") == "preauthorized":
        result = await rm._submit_job(state, **submit_args)
    record("submit", result)
    require(result.get("status") in {"success", "submission_outcome_unknown"}, result)
    jid = str(result.get("job_id") or "")
    nonce = result.get("submission_nonce") or result.get("route_attempt_id")
    require(nonce, result)
    recovered = recovery.reconcile_external_submission(state, nonce)
    record("recovery", recovered)
    require(recovered.get("status") == "success", recovered)
    jid = str(recovered["resolved_identity"]["job_id"])
    for i in range(15):
        status = await rm._job_status(
            state, "slurm", jid, submission_nonce=nonce, collect_outputs=True
        )
        if i == 0:
            record("first_status", status)
        if status.get("output_collection", {}).get("ok") is True:
            break
        await asyncio.sleep(1)
    record("terminal_and_return", status)
    require(status.get("output_collection", {}).get("ok") is True, status)
    terminals = [
        row
        for row in ledger.open_ledger(state).records()
        if row.get("kind") == "job_terminal" and row.get("nonce") == nonce
    ]
    require(
        len(terminals) == 1
        and terminals[0].get("derived_state") == "COMPLETED"
        and terminals[0].get("exit_code") == 0,
        terminals,
    )
    accounting = slurm_ssh.run(
        target, ["sacct", "-X", "-n", "-P", "--format=JobIDRaw,Comment%256,Cluster"]
    )
    matches = [
        line
        for line in accounting.get("stdout", "").splitlines()
        if ("|ai4s:" + nonce + "|") in line
    ]
    record("nonce_accounting", {"matches": matches})
    require(len(matches) == 1, matches)
    returned = list(state.root.rglob("*result.bin"))
    require(returned, status)
    require(any(p.read_bytes() == inp.read_bytes() for p in returned), returned)
    record(
        "returned_digest",
        {"sha256": hashlib.sha256(inp.read_bytes()).hexdigest(), "copies": len(returned)},
    )
    repeat = await rm._job_status(state, "slurm", jid, submission_nonce=nonce, collect_outputs=True)
    record("repeat_collection", repeat)
    require(repeat.get("output_collection", {}).get("ok") is True, repeat)
    name2 = "cancel-" + uuid.uuid4().hex[:8]
    remote2 = remote_base + "/" + name2
    require(slurm_ssh.run(target, ["mkdir", "-p", remote2]).get("ok"), remote2)
    cancel_args = dict(
        command="sleep 60",
        scheduler="slurm",
        slurm_target=target,
        job_name=name2,
        workdir=remote2,
        output_dir=remote2,
        output_paths=[remote2],
        memory_gb=0.0625,
        storage_gb=0.01,
        walltime_minutes=2,
        dry_run=False,
    )
    second = await rm._submit_job(state, **cancel_args)
    if second.get("status") == "preauthorized":
        second = await rm._submit_job(state, **cancel_args)
    record("cancel_submit", second)
    require(second.get("status") == "success", second)
    nonce2 = second["submission_nonce"]
    jid2 = str(second["job_id"])
    cancelled = await rm._cancel_job(
        state, "slurm", jid2, submission_nonce=nonce2, reason="authorized lifecycle acceptance"
    )
    if cancelled.get("status") == "preauthorized":
        cancelled = await rm._cancel_job(
            state, "slurm", jid2, submission_nonce=nonce2, reason="authorized lifecycle acceptance"
        )
    record("cancel", cancelled)
    raw = slurm_ssh.run(target, ["sacct", "-j", jid2, "--format=JobID,State,ExitCode", "-n", "-P"])
    record("cancel_accounting", raw)
    require(raw.get("ok") and "CANCELLED" in raw.get("stdout", ""), raw)
    record("complete", {"ok": True, "job_ids": [jid, jid2], "state_root": str(state.root)})


if __name__ == "__main__":
    asyncio.run(main())
