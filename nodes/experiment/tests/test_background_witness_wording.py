"""Background-launch witness must not promise host-dependent process reaping."""
from __future__ import annotations

import inspect

from nodes.experiment.tools import safe_bash


def test_background_witness_describes_host_dependent_liveness_without_changing_gate():
    source = inspect.getsource(safe_bash)

    assert "后台进程是否活过本次调用取决于主机" in source
    assert "无 systemd scope 时可能存活并继续写可写根" in source
    assert "需要跨调用存活的工作请用 submit_job" in source
    assert "后台进程不会活过本次调用" not in source
