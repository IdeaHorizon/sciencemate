import json
import sys
import threading

import pytest

from core.state import State
from nodes.experiment.tools import build_resource_guard as guard
from nodes.experiment.tools import resource_manager as rm


@pytest.mark.parametrize("read_fails", [False, True])
def test_real_build_pipes_preserve_bytes_and_report_read_failure(tmp_path, monkeypatch, read_fails):
    monkeypatch.setattr(guard, "host_memory_admission_block", lambda _: None)
    monkeypatch.setattr(guard, "filesystem_free_bytes", lambda _: 10**12)
    monkeypatch.setattr(guard, "wrap_with_cgroup", lambda argv, _: (argv, None, "fake.scope"))
    monkeypatch.setattr(guard, "_unit_usage", lambda _: {"TasksCurrent": 1, "MemoryCurrent": 1})
    monkeypatch.setattr(guard, "wait_for_cgroup_quiescence", lambda *a, **k: {"status": "quiet"})
    monkeypatch.setattr(guard, "kill_guarded_tree", lambda _, fallback: fallback())
    if read_fails:
        def failed_read(_):
            raise OSError("injected pipe read failure")
        monkeypatch.setattr(guard, "_read_build_pipe", failed_read)
    started = []
    original = threading.Thread

    def thread(*a, **kw):
        result = original(*a, **kw)
        started.append(result)
        return result
    monkeypatch.setattr(guard.threading, "Thread", thread)
    status = tmp_path / "status.json"
    code = "import sys; sys.stdout.buffer.write('日志✓'.encode()); sys.stderr.buffer.write(b'err')"
    rc = guard.supervise_local_build(
        [sys.executable, "-c", code], stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err", status_path=status,
        limits=guard.BuildLimits(1, 64, 8_000, 10_000, 2_000, 10, 20_000, 2_000),
    )
    result = json.loads(status.read_text())
    assert all(not reader.is_alive() for reader in started)
    if read_fails:
        assert rc == 125 and result["status"] == "error"
        assert result["reason"] == "build_log_read_failed"
        assert "injected pipe read failure" in result["detail"]
    else:
        assert rc == 0 and result["status"] == "success"
        assert (tmp_path / "out").read_bytes() == "日志✓".encode()
        assert (tmp_path / "err").read_bytes() == b"err"


def test_stage_in_explicit_mode_is_bound_independently_of_source_bits(tmp_path):
    state = State.new("experiment", tmp_path / "runs")
    source = state.root / "solver.sh"
    source.write_bytes(b"#!/bin/sh\nexit 0\n")
    source.chmod(0o644)
    items = [{"src": str(source), "dst": "solver.sh", "mode": "0755"}]
    executable = rm._validate_stage_in(state, items)
    readonly = rm._validate_stage_in(state, [{**items[0], "mode": "0644"}])
    assert executable[0]["mode"] == "0755"
    assert readonly[0]["mode"] == "0644"
    assert executable[0]["sha256"] == readonly[0]["sha256"]
    assert executable != readonly
    with pytest.raises(ValueError, match="mode must be"):
        rm._validate_stage_in(state, [{**items[0], "mode": "4755"}])
