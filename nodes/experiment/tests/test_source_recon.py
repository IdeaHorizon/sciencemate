"""Generic source reconnaissance tests."""
from __future__ import annotations

import json

from core.state import State
from nodes.experiment.tools import safe_bash
from nodes.experiment.tools.source_recon import scan_source


def test_generic_configuration_and_orchestration_evidence_suggests_official_route(tmp_path):
    source = tmp_path / "source"
    (source / "workflow_config").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "scripts" / "create_case.py").write_text("print('orchestrate')\n")

    report = scan_source(str(source))

    candidates = [
        item for item in report["route_candidates"]
        if item["route"] == "official_build_system"
    ]
    assert len(candidates) == 1
    assert candidates[0]["confidence"] == "low"
    assert "workflow_config" in candidates[0]["evidence"]
    assert "scripts/create_case.py" in candidates[0]["entrypoints"]
    assert "官方构建文档" in candidates[0]["limitations"]


def test_autogen_recon_scans_source_and_passes_separate_build_root(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    build = tmp_path / "build"
    source.mkdir()
    build.mkdir()
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\nproject(example C)\n",
        encoding="utf-8",
    )
    (build / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    state = State.new("experiment", tmp_path / "runs")
    captured = {}

    monkeypatch.setattr(
        safe_bash,
        "bench_enabled",
        lambda capability: capability == "provision_first",
    )
    monkeypatch.setattr(
        safe_bash,
        "_recon_fingerprint",
        lambda *_args, **_kwargs: "source-build-pair",
    )

    def capture_graph(_state, source_path, build_root=None):
        captured["source_path"] = source_path
        captured["build_root"] = build_root
        return {"status": "unknown"}

    monkeypatch.setattr(safe_bash, "_autogen_build_graph", capture_graph)

    status = safe_bash._autogen_recon(
        state,
        str(source),
        build_root=str(build),
    )

    assert status == "generated"
    recon_summary = state.list_artifacts("source_recon")
    assert len(recon_summary) == 1
    recon = state.read_artifact(recon_summary[0]["id"])
    content = json.loads(recon["content"])
    assert content["source_path"] == str(source.resolve())
    assert "CMakeLists.txt" in json.dumps(content)
    assert captured == {
        "source_path": str(source),
        "build_root": str(build),
    }


def test_one_generic_hint_alone_does_not_guess_an_official_route(tmp_path):
    source = tmp_path / "source"
    (source / "workflow_config").mkdir(parents=True)

    report = scan_source(str(source))

    assert all(
        item["route"] != "official_build_system"
        for item in report["route_candidates"]
    )
