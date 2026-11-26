"""Experiment plans resources; Core alone constructs and owns process groups."""

import ast
from pathlib import Path


def test_experiment_resource_planner_has_no_second_process_supervisor():
    path = Path(__file__).resolve().parents[1] / "nodes/experiment/tools/build_resource_guard.py"
    tree = ast.parse(path.read_text())
    raw_spawns = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr in {"run", "Popen", "call"}
    ]
    assert raw_spawns == [], f"node-owned lifecycle remains at lines {raw_spawns}"


def test_retired_supervisor_entry_does_not_report_a_successful_execution(tmp_path):
    import subprocess
    import sys

    path = Path(__file__).resolve().parents[1] / "nodes/experiment/tools/build_resource_guard.py"
    marker = tmp_path / "must-not-run"
    result = subprocess.run(
        [
            sys.executable,
            str(path),
            "supervise",
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path;Path({str(marker)!r}).touch()",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "Core managed tools" in result.stderr
    assert not marker.exists()
