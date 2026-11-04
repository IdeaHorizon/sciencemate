"""Collected logs identify their actual bytes and retain both native streams."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from nodes.experiment.tools import safe_bash as sb
from test_timeout_escalation import requires_sandbox, runattempt_state  # noqa: F401


@requires_sandbox
@pytest.mark.parametrize("exit_code", [0, 19], ids=["success", "failed-child"])
def test_real_command_log_digest_covers_full_unicode_streams_and_exit_code(
    runattempt_state, exit_code,
):
    state = runattempt_state
    run_root = sb.experiment_output_dir(state, "runtime", create=True).resolve()
    state.hook_state["path_roles"] = {"run_root": {"path": str(run_root), "writable": True}}
    # Bash's printf emits these UTF-8 octets independently of locale. Both
    # streams exceed the public tails, so checking only a tail cannot pass.
    stdout = "输出 begin\n" + " " * 4096 + "\nstdout end\n"
    stderr = "诊断 begin\n" + " " * 2048 + "\nstderr end\n"
    command = (
        r"printf '\350\276\223\345\207\272 begin\n'; "
        r"printf '%4096s' ''; printf '\nstdout end\n'; "
        r"printf '\350\257\212\346\226\255 begin\n' >&2; "
        r"printf '%2048s' '' >&2; printf '\nstderr end\n' >&2; "
        f"exit {exit_code}"
    )

    result = asyncio.run(sb._exec_and_log(
        state, command, cwd=str(run_root), timeout=30, sandbox_profile="bash"))

    assert result["returncode"] == exit_code, result
    assert result["status"] == ("success" if exit_code == 0 else "error"), result
    assert result["stdout_tail"] == stdout[-sb._STDOUT_TAIL:]
    assert result["stderr_tail"] == stderr[-sb._STDERR_TAIL:]
    assert result["truncated"] is True
    log = Path(result["log_path"]).read_bytes()
    assert hashlib.sha256(log).hexdigest() == result["log_sha256"]
    assert stdout.encode("utf-8") in log
    assert stderr.encode("utf-8") in log
    assert f"# returncode: {exit_code}".encode("ascii") in log
