"""Recovery belongs to the installed host, not to the selected source revision."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parents[1]


def stranded_installation(tmp_path):
    source = tmp_path / "legacy-source"
    app = source / "platform/backend/app"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("")
    (app / "launcher.py").write_text("raise RuntimeError('legacy checkout was loaded')\n")
    (source / "core").mkdir()
    (source / "core/agent_loop.py").write_text("")
    (source / ".git").mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (data / "developer.json").write_text(json.dumps({"source_root": str(source)}))
    # Recovery must not open or migrate this database, or touch projects/settings.
    for name in ["db.sqlite", "settings.json", "project-worktrees/p1/research.txt"]:
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep exactly these bytes")
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "PYTHONDONTWRITEBYTECODE": "1",
           "PLATFORM_DATA_ROOT": str(data), "HARNESS_FRAMEWORK_HOME": str(data)}
    return data, app.parent, env


def assert_recovered(data):
    assert not (data / "developer.json").exists()
    for name in ["db.sqlite", "settings.json", "project-worktrees/p1/research.txt"]:
        assert (data / name).read_bytes() == b"keep exactly these bytes"


def test_installed_recovery_runs_before_legacy_source_or_pending_payload(tmp_path):
    data, _source_cwd, env = stranded_installation(tmp_path)
    staged = data / "payload/staged.json"
    staged.parent.mkdir()
    staged.write_text('{"version":"999.0.0"}')
    command = [sys.executable, "-B", "-m", "app.launcher", "reset-developer-mode"]
    for attempt in range(2):
        done = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Developer mode is off" in done.stdout
        assert_recovered(data)
        assert staged.read_text() == '{"version":"999.0.0"}', "Recovery must not apply an update"


@pytest.fixture(scope="module")
def mac_recovery(tmp_path_factory):
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("requires native macOS Swift toolchain")
    work = tmp_path_factory.mktemp("mac-developer-recovery")
    source = (ROOT / "platform/desktop/mac/Shell.swift").read_text().split("// MARK: - 应用", 1)[0]
    swift = work / "Recovery.swift"
    swift.write_text(source + '''
do {
    try Backend.resetDeveloperMode(python: URL(fileURLWithPath: CommandLine.arguments[1]))
} catch {
    fputs(error.localizedDescription, stderr)
    exit(1)
}
''')
    executable = work / "Recovery"
    compiled = subprocess.run(["swiftc", str(swift), "-o", str(executable)],
                              capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr
    return executable


def test_mac_native_recovery_cannot_import_the_legacy_source_cwd(mac_recovery, tmp_path):
    data, source_cwd, env = stranded_installation(tmp_path)
    done = subprocess.run([str(mac_recovery), sys.executable], cwd=source_cwd, env=env,
                          capture_output=True, text=True, timeout=15)
    assert done.returncode == 0, done.stdout + done.stderr
    assert_recovered(data)


def test_mac_recovery_reports_failure_and_keeps_the_declaration(mac_recovery, tmp_path):
    data, source_cwd, env = stranded_installation(tmp_path)
    declaration = (data / "developer.json").read_bytes()
    data.chmod(0o500)
    try:
        done = subprocess.run([str(mac_recovery), sys.executable], cwd=source_cwd, env=env,
                              capture_output=True, text=True, timeout=15)
        assert done.returncode != 0
        assert "PermissionError" in done.stderr
        assert (data / "developer.json").read_bytes() == declaration
    finally:
        data.chmod(0o700)
