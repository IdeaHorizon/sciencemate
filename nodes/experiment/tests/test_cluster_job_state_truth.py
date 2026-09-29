"""Cluster/native job terminal facts come from the backend that can observe them.

The scheduler tests put executable qstat/squeue/sacct stand-ins at the front of
PATH.  They intentionally exercise the production command runner and the
``_job_status_sync -> _scheduler_phase -> _observed_job_end`` path.
"""
from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import external_submission_recovery as recovery
from nodes.experiment.tools import operation_completion as completion
from nodes.experiment.tools import resource_manager as manager


def _command(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")


def _reset_pbs_probe() -> None:
    reset = getattr(manager, "_reset_pbs_flavor_cache_for_tests", None)
    if reset is not None:
        reset()


@pytest.fixture(autouse=True)
def _isolate_pbs_probe():
    _reset_pbs_probe()
    yield
    _reset_pbs_probe()


def _state(tmp_path: Path) -> State:
    return State.new("experiment", tmp_path / "state")


def test_pbs_pro_completed_job_is_observed_with_exit_status(
    tmp_path: Path, monkeypatch,
):
    log = tmp_path / "qstat.argv"
    monkeypatch.setenv("FAKE_QSTAT_LOG", str(log))
    _command(tmp_path, "qstat", r'''
        import os
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        with Path(os.environ["FAKE_QSTAT_LOG"]).open("a", encoding="utf-8") as stream:
            stream.write(" ".join(args) + "\n")
        if args == ["--version"]:
            print("pbs_version = 2022.1.1")
        elif args == ["-x", "-f", "41.server"]:
            print("Job Id: 41.server")
            print("    job_state = F")
            print("    Exit_status = 0")
        elif args == ["-f", "41.server"]:
            print("qstat: Job has finished, use -x or -H", file=sys.stderr)
            raise SystemExit(35)
        else:
            print(f"unexpected qstat args: {args!r}", file=sys.stderr)
            raise SystemExit(64)
    ''')
    _fake_path(monkeypatch, tmp_path)
    _reset_pbs_probe()

    ended = asyncio.run(manager._observed_job_end(
        _state(tmp_path), {"scheduler": "pbs", "job_id": "41.server"},
    ))
    snapshot = manager._scheduler_environment_snapshot("pbs", "41.server", None)

    assert ended is not None, ended
    assert ended["source"] == "pbs_job_state"
    assert ended["exit_code"] == 0
    assert ended["succeeded"] is True
    assert snapshot["job_snapshot"]["pbs_query_mode"] == "history"
    assert log.read_text(encoding="utf-8").splitlines() == [
        "--version", "-x -f 41.server", "-x -f 41.server",
    ]


def test_torque_status_and_recovery_never_use_xml_x(
    tmp_path: Path, monkeypatch,
):
    log = tmp_path / "qstat.argv"
    monkeypatch.setenv("FAKE_QSTAT_LOG", str(log))
    _command(tmp_path, "qstat", r'''
        import os
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        with Path(os.environ["FAKE_QSTAT_LOG"]).open("a", encoding="utf-8") as stream:
            stream.write(" ".join(args) + "\n")
        if args == ["--version"]:
            print("Version: 6.1.3")
        elif "-x" in args:
            print("<Data><Job><Job_Id>77.server</Job_Id></Job></Data>")
        elif args == ["-f", "77.server"]:
            print("Job Id: 77.server")
            print("    job_state = R")
        elif args == ["-f"]:
            print("Job Id: 77.server")
            print("    Variable_List = AI4S_SUBMISSION_NONCE=ai4s:nonce-77")
        else:
            print(f"unexpected qstat args: {args!r}", file=sys.stderr)
            raise SystemExit(64)
    ''')
    _fake_path(monkeypatch, tmp_path)
    _reset_pbs_probe()

    status = manager._job_status_sync("pbs", "77.server", None)
    recovered = recovery._query_pbs(
        {"submission_nonce": "nonce-77"}, recovery._run_query,
    )

    assert manager._scheduler_phase("pbs", status) == "running"
    assert recovered["query_status"] == "unique", recovered
    assert recovered["identities"][0]["job_id"] == "77.server"
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.count("--version") == 1
    assert all("-x" not in call.split() for call in calls)


def test_unknown_pbs_flavor_keeps_status_active_but_recovers_legacy_history(
    tmp_path: Path, monkeypatch,
):
    log = tmp_path / "qstat.argv"
    monkeypatch.setenv("FAKE_QSTAT_LOG", str(log))
    _command(tmp_path, "qstat", r'''
        import os
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        with Path(os.environ["FAKE_QSTAT_LOG"]).open("a", encoding="utf-8") as stream:
            stream.write(" ".join(args) + "\n")
        if args == ["--version"]:
            print("qstat build 1.2")
        elif args == ["-f", "78.server"]:
            print("Job Id: 78.server")
            print("    job_state = R")
        elif args == ["-f"]:
            pass
        elif args == ["-x", "-f"]:
            print("Job Id: 78.server")
            print("    job_state = F")
            print("    Variable_List = AI4S_SUBMISSION_NONCE=ai4s:nonce-78")
        else:
            raise SystemExit(64)
    ''')
    _fake_path(monkeypatch, tmp_path)

    status = manager._job_status_sync("pbs", "78.server", None)
    recovered = recovery._query_pbs(
        {"submission_nonce": "nonce-78"}, recovery._run_query,
    )

    assert manager._scheduler_phase("pbs", status) == "running"
    assert recovered["query_status"] == "unique"
    assert recovered["identities"][0]["job_id"] == "78.server"
    assert log.read_text(encoding="utf-8").splitlines() == [
        "--version",
        "-f 78.server",
        "--version",
        "-f",
        "-x -f",
    ]


def test_pbs_history_disabled_falls_back_to_unknown_with_reason(
    tmp_path: Path, monkeypatch,
):
    log = tmp_path / "qstat.argv"
    monkeypatch.setenv("FAKE_QSTAT_LOG", str(log))
    _command(tmp_path, "qstat", r'''
        import os
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        with Path(os.environ["FAKE_QSTAT_LOG"]).open("a", encoding="utf-8") as stream:
            stream.write(" ".join(args) + "\n")
        if args == ["--version"]:
            print("pbs_version = 2022.1.1")
        elif args == ["-x", "-f", "42.server"]:
            print("qstat: PBS is not configured to maintain job history", file=sys.stderr)
            raise SystemExit(153)
        elif args == ["-f", "42.server"]:
            print("qstat: Unknown Job Id 42.server", file=sys.stderr)
            raise SystemExit(35)
        elif args == ["-x", "-f"]:
            print("qstat: PBS is not configured to maintain job history", file=sys.stderr)
            raise SystemExit(153)
        elif args == ["-f"]:
            pass
        else:
            print(f"unexpected qstat args: {args!r}", file=sys.stderr)
            raise SystemExit(64)
    ''')
    _fake_path(monkeypatch, tmp_path)
    _reset_pbs_probe()

    status = manager._job_status_sync("pbs", "42.server", None)
    recovered = recovery._query_pbs(
        {"submission_nonce": "nonce-42"}, recovery._run_query,
    )

    assert manager._scheduler_phase("pbs", status) == "unknown"
    assert status["raw"]["reason"] == "pbs_history_not_configured"
    assert status["raw"]["pbs_history_available"] is False
    assert recovered["query_status"] == "query_error"
    assert recovered["reason"] == "pbs_history_not_configured_after_active_zero"
    assert log.read_text(encoding="utf-8").splitlines() == [
        "--version",
        "-x -f 42.server",
        "-f 42.server",
        "-x -f",
        "-f",
    ]


def test_unknown_pbs_flavor_is_reprobed_and_known_flavor_is_cached():
    probes = iter([
        {"ok": False, "returncode": 127, "stdout": "", "stderr": "not found"},
        {"ok": True, "returncode": 0,
         "stdout": "pbs_version = 2022.1.1", "stderr": ""},
    ])
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(list(argv))
        return next(probes)

    first = manager.pbs_flavor(runner)
    second = manager.pbs_flavor(runner)
    third = manager.pbs_flavor(
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("known PBS flavor must be cached")),
    )

    assert first["flavor"] == "unknown"
    assert second["flavor"] == "pbs_pro_openpbs"
    assert third["flavor"] == "pbs_pro_openpbs"
    assert calls == [["qstat", "--version"], ["qstat", "--version"]]


def test_slurm_allocation_decides_terminal_while_steps_supply_oom_and_maxrss(
    tmp_path: Path, monkeypatch,
):
    sacct_log = tmp_path / "sacct.argv"
    monkeypatch.setenv("FAKE_SACCT_LOG", str(sacct_log))
    _command(tmp_path, "squeue", r'''
        import sys
        if sys.argv[1:] != ["-j", "123", "-h", "-o", "%i|%T|%M|%R"]:
            raise SystemExit(64)
    ''')
    _command(tmp_path, "scontrol", r'''
        import sys
        if sys.argv[1:] == ["--version"]:
            print("slurm 24.05.4")
        elif sys.argv[1:] == ["show", "job", "-o", "123"]:
            print("JobId=123 JobState=COMPLETED")
        else:
            raise SystemExit(64)
    ''')
    _command(tmp_path, "sacct", r'''
        import os
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        with Path(os.environ["FAKE_SACCT_LOG"]).open("a", encoding="utf-8") as stream:
            stream.write(" ".join(args) + "\n")
        if "-X" in args:
            print("123|COMPLETED|0:0||||||")
        else:
            print("123|COMPLETED|0:0||||||")
            print("123.batch|COMPLETED|0:0||||||512M")
            print("123.0|OUT_OF_MEMORY|0:125||||||2G")
    ''')
    _fake_path(monkeypatch, tmp_path)
    record = {
        "scheduler": "slurm",
        "job_id": "123",
        "expected_termination": {"exit_codes": [0], "task_quote": "exit 0"},
    }

    ended = asyncio.run(manager._observed_job_end(_state(tmp_path), record))
    snapshot = manager._scheduler_environment_snapshot("slurm", "123", None)
    success = completion._external_job_success_evidence(
        record,
        {
            "status": "success",
            "scheduler_phase": "terminal",
            "health_state": "terminal_needs_analysis",
            "error_evidence": [],
            "completion_paths": [],
            "terminal_evidence": ended,
        },
        {"status": None},
    )

    assert ended is not None, ended
    assert ended["allocation_state"] == "COMPLETED"
    assert ended["returncode"] == 0
    assert ended["succeeded"] is True
    assert ended["allocation_rows"] == [{
        "job_id": "123", "state": "COMPLETED", "returncode": 0,
        "exit_code": 0, "exit_signal": 0,
    }]
    assert ended["oom_killed"] is True
    assert ended["oom_steps"] == ["123.0"]
    assert ended["termination_matched"] is False
    assert success["verified"] is True
    assert success["succeeded"] is True
    assert success["oom_killed"] is True
    assert success["termination_matched"] is False
    assert snapshot["max_rss"] == "2G"
    assert snapshot["max_rss_source"]["job_id"] == "123.0"
    assert all("-X" not in call.split()
               for call in sacct_log.read_text(encoding="utf-8").splitlines())


def test_slurm_terminal_step_does_not_end_a_running_allocation(
    tmp_path: Path, monkeypatch,
):
    _command(tmp_path, "squeue", r'''
        import sys
        if sys.argv[1:] != ["-j", "124", "-h", "-o", "%i|%T|%M|%R"]:
            raise SystemExit(64)
    ''')
    _command(tmp_path, "sacct", r'''
        print("124|RUNNING|0:0||||||")
        print("124.0|OUT_OF_MEMORY|0:125||||||1G")
    ''')
    _fake_path(monkeypatch, tmp_path)

    ended = asyncio.run(manager._observed_job_end(
        _state(tmp_path), {"scheduler": "slurm", "job_id": "124"},
    ))

    assert ended is None


def test_slurm_array_waits_for_every_allocation_before_terminal(monkeypatch):
    def row(job_id: str, state: str, exit_code: str, max_rss: str = "") -> str:
        return "|".join([
            job_id, state, exit_code, "", "", "", "", "", max_rss,
        ])

    rows = [
        row("1234", "COMPLETED", "0:0"),
        row("1234.batch", "COMPLETED", "0:0", "1G"),
        row("1235", "RUNNING", "0:0"),
        row("1235.batch", "RUNNING", "0:0", "2G"),
    ]
    monkeypatch.setattr(
        manager, "_run",
        lambda _argv, **_kwargs: {
            "ok": True, "returncode": 0,
            "stdout": "\n".join(rows) + "\n", "stderr": "",
        },
    )

    assert manager._remote_job_ended_on_its_own(
        "slurm", {"ok": True, "stdout": ""}, "1234",
    ) is None

    rows[2] = row("1235", "COMPLETED", "0:0")
    rows[3] = row("1235.batch", "COMPLETED", "0:0", "2G")
    ended = manager._remote_job_ended_on_its_own(
        "slurm", {"ok": True, "stdout": ""}, "1234",
    )

    assert ended is not None
    assert ended["terminal"] is True
    assert ended["succeeded"] is True
    assert ended["returncode"] == 0
    assert ended["allocation_state"] is None
    assert ended["allocation_states"] == ["COMPLETED", "COMPLETED"]
    assert ended["allocation_rows"] == [
        {"job_id": "1234", "state": "COMPLETED", "returncode": 0,
         "exit_code": 0, "exit_signal": 0},
        {"job_id": "1235", "state": "COMPLETED", "returncode": 0,
         "exit_code": 0, "exit_signal": 0},
    ]


def test_slurm_array_aggregates_exit_oom_and_maxrss_across_children(monkeypatch):
    def row(job_id: str, state: str, exit_code: str, max_rss: str = "") -> str:
        return "|".join([
            job_id, state, exit_code, "", "", "", "", "", max_rss,
        ])

    rows = [
        row("1234", "COMPLETED", "0:0"),
        row("1234.batch", "COMPLETED", "0:0", "1G"),
        row("1235", "FAILED", "7:0"),
        row("1235.batch", "OUT_OF_MEMORY", "0:125", "3G"),
        row("1236", "FAILED", "9:0"),
    ]
    monkeypatch.setattr(
        manager, "_run",
        lambda _argv, **_kwargs: {
            "ok": True, "returncode": 0,
            "stdout": "\n".join(rows) + "\n", "stderr": "",
        },
    )

    ended = manager._remote_job_ended_on_its_own(
        "slurm", {"ok": True, "stdout": ""}, "1234",
    )

    assert ended is not None
    assert ended["terminal"] is True
    assert ended["succeeded"] is False
    assert ended["returncode"] == 7
    assert ended["oom_killed"] is True
    assert ended["oom_steps"] == ["1235.batch"]
    assert ended["max_rss"] == "3G"
    assert ended["max_rss_source"]["job_id"] == "1235.batch"

    success = completion._external_job_success_evidence(
        {"scheduler": "slurm", "job_id": "1234"},
        {"terminal_evidence": ended},
        {"status": None},
    )
    assert success["verified"] is True
    assert success["succeeded"] is False
    assert success["allocation_rows"] == ended["allocation_rows"]
    assert success["oom_killed"] is True
    assert success["oom_steps"] == ["1235.batch"]
    assert success["max_rss"] == "3G"
    assert success["max_rss_bytes"] == 3 * 1024 ** 3
    assert success["max_rss_source"] == {
        "source": "slurm_sacct_step", "job_id": "1235.batch",
    }


@pytest.mark.parametrize(
    "oom_field", [{}, {"oom_killed": "true"}, {"oom_killed": 1}],
    ids=["missing", "non_boolean_string", "non_boolean_integer"],
)
def test_native_local_oom_is_unobservable_without_changing_the_verdict(
    tmp_path: Path, monkeypatch, oom_field,
):
    record = {
        "scheduler": "local",
        "job_id": "hf-job-native-fact",
        "container_runtime_id": "c" * 64,
        "expected_termination": {"exit_codes": [3], "task_quote": "exit 3"},
    }
    health = {
        "status": "success",
        "scheduler_phase": "terminal",
        "health_state": "terminal_needs_analysis",
        "error_evidence": [],
        "completion_paths": [],
        "scheduler_result": {"status": "success", "raw": {
            "ok": True,
            "sandbox_state": {
                "exists": True,
                "id": "c" * 64,
                "name": "hf-job-native-fact",
                "managed": True,
                "kind": "job",
                "running": False,
                "status": "exited",
                "exit_code": 3,
                **oom_field,
            },
        }},
    }

    evidence = completion._external_job_success_evidence(
        record, health, {"status": None},
    )
    monkeypatch.setattr(
        "core.isolation.enforcement_snapshot",
        lambda: {"backend": "linux", "enforced": ["write_boundary"]},
    )
    monkeypatch.setattr(
        "core.sandbox.inspect_container",
        lambda _job_id: dict(health["scheduler_result"]["raw"]["sandbox_state"]),
    )
    monkeypatch.setattr(
        manager, "_execution_probe",
        lambda argv, **_kwargs: (_ for _ in ()).throw(
            AssertionError(f"local snapshot must not execute {argv!r}")),
    )
    snapshot = manager._scheduler_environment_snapshot(
        "local", "hf-job-native-fact", None,
    )

    assert evidence["verified"] is True
    assert evidence["succeeded"] is False  # exit 3 remains a physical failure
    assert evidence["termination_matched"] is True  # declared exit 3 still matches
    assert evidence["oom_killed"] is None
    assert evidence["oom_observable"] is False
    assert evidence["source"] == "native_job_record"
    assert snapshot["backend_identity"] == {
        "kind": "native_managed_job",
        "backend": "linux",
        "source": "core.isolation.enforcement_snapshot",
    }


@pytest.mark.parametrize(
    "oom_killed, termination_matched", [(False, True), (True, False)],
    ids=["not_oom", "oom"],
)
def test_native_local_explicit_oom_is_observed(
    oom_killed, termination_matched,
):
    record = {
        "scheduler": "local",
        "job_id": "hf-job-native-oom",
        "container_runtime_id": "d" * 64,
        "expected_termination": {"exit_codes": [3], "task_quote": "exit 3"},
    }
    health = {
        "status": "success",
        "scheduler_phase": "terminal",
        "health_state": "terminal_needs_analysis",
        "error_evidence": [],
        "completion_paths": [],
        "scheduler_result": {"status": "success", "raw": {
            "ok": True,
            "sandbox_state": {
                "exists": True,
                "id": "d" * 64,
                "name": "hf-job-native-oom",
                "managed": True,
                "kind": "job",
                "running": False,
                "status": "exited",
                "exit_code": 3,
                "oom_killed": oom_killed,
            },
        }},
    }

    evidence = completion._external_job_success_evidence(
        record, health, {"status": None},
    )

    assert evidence["verified"] is True
    assert evidence["succeeded"] is False
    assert evidence["termination_matched"] is termination_matched
    assert evidence["oom_killed"] is oom_killed
    assert evidence["oom_observable"] is True


# ── 046：调度器杀掉的作业不算成功，也不算"符合预期终止" ──────────────────────
#
# sacct 的 ExitCode 是 <退出码>:<信号>。作业被调度器结束时程序没机会给出自己的
# 退出码，低位恒为 0 —— 只看低位会把 TIMEOUT / CANCELLED / NODE_FAIL / PREEMPTED
# 读成 exit 0。下面两组分别钉产生端与两条收货门。

def _sacct_rows(*rows: tuple[str, str, str]) -> dict[str, object]:
    """把 (JobIDRaw, State, ExitCode) 渲染成 sacct -n -P 的一行。"""
    fields = manager._SLURM_ACCOUNTING_FIELDS
    lines = []
    for job_id, state, exit_code in rows:
        values = {"JobIDRaw": job_id, "State": state, "ExitCode": exit_code}
        lines.append("|".join(str(values.get(name, "")) for name in fields))
    return {"ok": True, "returncode": 0, "stdout": "\n".join(lines), "stderr": ""}


@pytest.mark.parametrize(
    ("state", "exit_code"),
    [
        ("TIMEOUT", "0:15"),
        ("TIMEOUT", "0:0"),
        ("CANCELLED by 1000", "0:15"),
        ("NODE_FAIL", "0:0"),
        ("PREEMPTED", "0:0"),
        ("DEADLINE", "0:0"),
        ("BOOT_FAIL", "0:0"),
        ("REVOKED", "0:0"),
        ("SPECIAL_EXIT", "0:0"),
        ("OUT_OF_MEMORY", "0:125"),
    ],
)
def test_scheduler_killed_allocation_is_not_a_success(state: str, exit_code: str):
    facts = manager._slurm_accounting_facts("1", _sacct_rows(("1", state, exit_code)))
    assert facts is not None
    assert facts["terminal"] is True, facts
    assert facts["succeeded"] is False, facts
    assert facts["scheduler_terminated"] is True, facts


@pytest.mark.parametrize(
    ("state", "exit_code", "succeeded"),
    [("COMPLETED", "0:0", True), ("FAILED", "1:0", False)],
)
def test_job_that_ended_on_its_own_is_not_scheduler_terminated(
    state: str, exit_code: str, succeeded: bool,
):
    facts = manager._slurm_accounting_facts("1", _sacct_rows(("1", state, exit_code)))
    assert facts["succeeded"] is succeeded, facts
    assert facts["scheduler_terminated"] is False, facts


def test_step_level_oom_still_does_not_deny_allocation_success():
    """已有决定不翻：step 行的 OOM 是诊断证据，不否定 allocation 的成功。"""
    facts = manager._slurm_accounting_facts(
        "1", _sacct_rows(("1", "COMPLETED", "0:0"), ("1.batch", "OUT_OF_MEMORY", "0:125")))
    assert facts["succeeded"] is True, facts
    assert facts["scheduler_terminated"] is False, facts
    assert facts["oom_killed"] is True, facts


def test_partially_timed_out_array_is_not_a_success():
    facts = manager._slurm_accounting_facts(
        "1", _sacct_rows(("1", "COMPLETED", "0:0"), ("2", "TIMEOUT", "0:0")))
    assert facts["succeeded"] is False, facts
    assert facts["scheduler_terminated"] is True, facts


def _slurm_health(facts: dict[str, object]) -> dict[str, object]:
    return {
        "status": "success",
        "scheduler_phase": "terminal",
        "health_state": "terminal_needs_analysis",
        "error_evidence": [],
        "completion_paths": [],
        "terminal_evidence": {"source": "slurm_accounting", **facts},
    }


@pytest.mark.parametrize(
    ("state", "exit_code"),
    [("TIMEOUT", "0:15"), ("NODE_FAIL", "0:0"), ("PREEMPTED", "0:0")],
)
def test_scheduler_kill_cannot_unlock_a_route_step(state: str, exit_code: str):
    """两条门：succeeded 与 termination_matched 都不得放行。

    NODE_FAIL / PREEMPTED 的 ExitCode 是 0:0，声明了 exit_codes=[0] 的作业会从
    "符合预期终止"这条侧门解锁 —— 只堵 succeeded 不够。
    """
    record = {
        "scheduler": "slurm",
        "job_id": "1",
        "expected_termination": {"exit_codes": [0], "task_quote": "exit 0"},
    }
    facts = manager._slurm_accounting_facts("1", _sacct_rows(("1", state, exit_code)))
    evidence = completion._external_job_success_evidence(
        record, _slurm_health(facts), {"status": None})

    assert evidence["verified"] is True, evidence
    assert evidence["succeeded"] is False, evidence
    assert evidence["scheduler_terminated"] is True, evidence
    assert evidence["termination_matched"] is False, evidence
    # route 收货门收的是 `succeeded is True or termination_matched is True`
    assert not (evidence.get("succeeded") is True
                or evidence.get("termination_matched") is True), evidence


def test_termination_verdict_is_single_sourced_for_every_producer():
    """termination_matched 有三个产生端；判据收口在 _termination_verdict 一处。"""
    record = {"expected_termination": {"exit_codes": [0], "task_quote": "exit 0"}}
    facts = manager._slurm_accounting_facts("1", _sacct_rows(("1", "NODE_FAIL", "0:0")))
    verdict = manager._termination_verdict(record, _slurm_health(facts))
    assert verdict["declared"] is True
    assert verdict["termination_matched"] is False, verdict
    assert verdict["source"] == "scheduler_terminated", verdict


def test_completed_job_still_matches_its_declared_termination():
    """收紧不得误伤：自己跑完的作业照常判"符合预期终止"。"""
    record = {"expected_termination": {"exit_codes": [0], "task_quote": "exit 0"}}
    facts = manager._slurm_accounting_facts("1", _sacct_rows(("1", "COMPLETED", "0:0")))
    health = _slurm_health(facts)
    health["scheduler_result"] = {"raw": {"sandbox_state": {"exit_code": 0}}}
    verdict = manager._termination_verdict(record, health)
    assert verdict["termination_matched"] is True, verdict


def test_unreadable_exit_code_does_not_fall_back_to_completion_paths():
    """ExitCode 为空时 slurm 分支跳过 —— 不能落到产物兜底把被杀的作业读成成功。

    被墙钟杀掉的作业照样可能留下部分产物；`declared_completion_paths` 那条兜底
    只看"声明的文件在不在、够不够新"。
    """
    import time

    record = {
        "scheduler": "slurm",
        "job_id": "1",
        "health_contract": {"completion_paths": ["/etc/hostname"]},
        "submitted_at": "2000-01-01T00:00:00+00:00",
    }
    facts = manager._slurm_accounting_facts("1", _sacct_rows(("1", "TIMEOUT", "")))
    assert facts["returncode"] is None, facts          # 触发跳过 slurm 分支的前提
    assert facts["scheduler_terminated"] is True, facts

    health = _slurm_health(facts)
    health["completion_paths"] = [
        {"path": "/etc/hostname", "exists": True, "mtime_epoch_s": time.time()},
    ]
    evidence = completion._external_job_success_evidence(record, health, {"status": None})
    assert evidence["verified"] is True, evidence
    assert evidence["succeeded"] is False, evidence
    assert evidence["source"] != "declared_completion_paths", evidence
