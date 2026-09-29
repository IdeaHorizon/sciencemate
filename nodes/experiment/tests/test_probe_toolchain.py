"""Regression coverage for the canonical experiment toolchain probe."""
from __future__ import annotations

import asyncio
import json

from core.state import State
from nodes.experiment.tools import probe_toolchain as pt


def test_probe_refreshes_canonical_platform_profile_under_build_env(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path)
    env = {"env_path": str(tmp_path / "env" / "build_env.sh"), "sha256": "env-sha"}
    calls: list[str] = []
    profile = {"schema_version": "1.0", "toolchain": {"gcc": {"path": "/opt/gcc"}}}

    monkeypatch.setattr(pt, "ensure_env_script", lambda got_state: env if got_state is state else None)
    monkeypatch.setattr(pt, "collect_platform_profile", lambda *, env_path: calls.append(env_path) or profile)

    result = asyncio.run(pt._probe_toolchain(state))

    assert result["status"] == "success"
    assert calls == [env["env_path"]]
    artifact = state.list_artifacts("platform_profile")[-1]
    record = state.read_artifact(artifact["id"])
    assert json.loads(record["content"]) == profile
    assert record["metadata"] == {
        "schema_version": "1.0", "generated_by": "probe_toolchain",
        "env_path": env["env_path"], "build_env_sha256": "env-sha",
    }


def test_probe_rejects_unknown_user_input_without_touching_environment(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path)
    monkeypatch.setattr(pt, "ensure_env_script", lambda _state: (_ for _ in ()).throw(AssertionError("must not prepare env")))

    result = asyncio.run(pt._probe_toolchain(state, command="command -v gcc"))

    assert result["status"] == "error"
    assert "accepts no input" in result["error"]
