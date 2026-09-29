from __future__ import annotations

from pathlib import Path


def test_bench_fixture_does_not_require_caller_to_classify_build_root():
    import yaml

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "bench_dryrun.yaml"
    data = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    assert "requires_build_root" not in data["node_inputs"]
