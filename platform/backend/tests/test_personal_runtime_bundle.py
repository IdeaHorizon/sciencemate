"""The personal desktop must ship an installable runtime without professional code."""

from __future__ import annotations

import importlib.util
import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_runtime_archive_contains_its_installer_and_core_but_no_local_state(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "personal_runtime_builder", REPO / "scripts/package/build_remote_runtime_bundle.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    tree = tmp_path / "checkout"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    sources = {
        "pyproject.toml": '[project]\nname="runtime"\nversion="1.2.3"\n',
        "platform/backend/app/main.py": "# API\n",
        "platform/backend/app/pro/internal.py": "# private edition\n",
        "core/agent_loop.py": "# agent\n",
        "platform_runtime.py": "# entry point\n",
        "deploy/personal/install-runtime.sh": "#!/bin/sh\nexit 0\n",
    }
    for name, content in sources.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "."], cwd=tree, check=True)
    (tree / "platform/backend/app/password.txt").write_text("private local file")
    monkeypatch.setattr(builder, "REPO", tree)
    output = builder.build()
    with tarfile.open(output) as archive:
        names = archive.getnames()
        assert "install-runtime.sh" in names
        assert "harness/core/agent_loop.py" in names
        assert "harness/platform_runtime.py" in names
        assert "harness/platform/backend/app/main.py" in names
        assert not any("password" in name or "/pro/" in name for name in names)
        assert archive.extractfile("harness/PAYLOAD_VERSION").read() == b"1.2.3"
        first_build = archive.extractfile("BUILD_ID").read()
        assert len(first_build) == 16
    # Reconnecting to a changed source checkout stages a new release, even before a version bump.
    (tree / "core/agent_loop.py").write_text("# updated agent\n")
    with tarfile.open(builder.build()) as archive:
        assert archive.extractfile("BUILD_ID").read() != first_build
