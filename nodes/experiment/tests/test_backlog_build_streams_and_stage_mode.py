
import pytest

from core.state import State
from nodes.experiment.tools import resource_manager as rm


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
