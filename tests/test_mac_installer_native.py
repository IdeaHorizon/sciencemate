"""Run the Mac installation engine on small bundles, outside real Applications/data."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Native macOS installer")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def installer(tmp_path_factory):
    work = tmp_path_factory.mktemp("mac-installer")
    main = work / "main.swift"
    main.write_text('''import Foundation
let args = CommandLine.arguments
let source = URL(fileURLWithPath: args[1])
let directories = args.dropFirst(2).map { URL(fileURLWithPath: $0, isDirectory: true) }
do {
    let result = try DesktopInstallation.install(source: source, directories: directories,
        preferredDirectory: directories[0], isRunning: { application in
            guard let path = ProcessInfo.processInfo.environment["SIMULATED_RUNNING"] else { return false }
            return DesktopInstallation.samePath(application, URL(fileURLWithPath: path))
        })
    let json: [String: Any] = ["path": result.application.path, "version": result.version, "changed": result.changed]
    print(String(data: try JSONSerialization.data(withJSONObject: json), encoding: .utf8)!)
} catch { fputs(error.localizedDescription, stderr); exit(1) }
''')
    executable = work / "installer"
    subprocess.run(["swiftc", str(ROOT / "platform/desktop/mac/Installation.swift"), str(main), "-o", str(executable)],
                   check=True, capture_output=True, text=True)
    return executable


def bundle(path, version, identity="com.ieit.sciencemate"):
    executable = path / "Contents/MacOS/ScienceMate"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": identity, "CFBundleShortVersionString": version,
        "CFBundleExecutable": "ScienceMate", "CFBundlePackageType": "APPL",
    }))
    (path / "payload.txt").write_text(version)
    return path


def run(installer, source, *directories, **env):
    return subprocess.run([str(installer), str(source), *map(str, directories)],
                          capture_output=True, text=True, env={**os.environ, **env}, timeout=30)


def test_first_install_then_upgrade_keeps_one_application(installer, tmp_path):
    applications = tmp_path / "Applications"
    first = bundle(tmp_path / "download-0.5.9.app", "0.5.9")
    result = run(installer, first, applications)
    assert result.returncode == 0, result.stderr
    installed = applications / "ScienceMate.app"
    assert json.loads(result.stdout)["changed"] is True
    assert (installed / "payload.txt").read_text() == "0.5.9"
    second = bundle(tmp_path / "download-0.5.10.app", "0.5.10")
    result = run(installer, second, applications)
    assert result.returncode == 0, result.stderr
    assert (installed / "payload.txt").read_text() == "0.5.10"
    assert list(applications.glob("*.app")) == [installed]
    assert not list(applications.glob(".ScienceMate-*.noindex"))
    assert first.exists() and second.exists()  # downloaded packages are not installed copies


@pytest.mark.parametrize("incoming", ["0.5.10", "0.5.9", "0.5.10+build.2"])
def test_equal_or_older_package_leaves_installation_untouched(installer, tmp_path, incoming):
    applications = tmp_path / "Applications"
    installed = bundle(applications / "ScienceMate.app", "0.5.10")
    source = bundle(tmp_path / "download.app", incoming)
    before = (installed / "Contents/Info.plist").read_bytes()
    result = run(installer, source, applications, SIMULATED_RUNNING=str(installed))
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert Path(outcome.pop("path")).resolve() == installed.resolve()
    assert outcome == {"version": "0.5.10", "changed": False}
    assert (installed / "Contents/Info.plist").read_bytes() == before
    assert (installed / "payload.txt").read_text() == "0.5.10"


def test_upgrade_finds_the_newest_copy_and_consolidates_old_names(installer, tmp_path):
    system = tmp_path / "system"
    user = tmp_path / "user"
    bundle(system / "ScienceMate 0.5.7.app", "0.5.7")
    bundle(user / "ScienceMate 0.5.9.app", "0.5.9")
    unrelated = bundle(user / "Other.app", "1.0", identity="other.product")
    source = bundle(tmp_path / "download.app", "0.5.10")
    result = run(installer, source, system, user)
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)["path"]).resolve() == (user / "ScienceMate.app").resolve()
    assert not list(system.glob("*.app"))
    assert sorted(p.name for p in user.glob("*.app")) == ["Other.app", "ScienceMate.app"]
    assert unrelated.exists()


def test_running_upgrade_is_refused_before_any_copy(installer, tmp_path):
    applications = tmp_path / "Applications"
    installed = bundle(applications / "ScienceMate.app", "0.5.9")
    source = bundle(tmp_path / "download.app", "0.5.10")
    result = run(installer, source, applications, SIMULATED_RUNNING=str(installed))
    assert result.returncode != 0
    assert (installed / "payload.txt").read_text() == "0.5.9"
    assert not list(applications.glob(".ScienceMate-*.noindex"))


def test_unrelated_destination_is_preserved(installer, tmp_path):
    applications = tmp_path / "Applications"
    unrelated = bundle(applications / "ScienceMate.app", "1.0", identity="other.product")
    source = bundle(tmp_path / "download.app", "0.5.10")
    result = run(installer, source, applications)
    assert result.returncode != 0
    assert (unrelated / "payload.txt").read_text() == "1.0"


def test_concurrent_install_is_refused_and_symlink_is_not_followed(installer, tmp_path):
    import fcntl
    applications = tmp_path / "Applications"
    applications.mkdir()
    source = bundle(tmp_path / "download.app", "0.5.10")
    external = bundle(tmp_path / "external.app", "0.5.7")
    (applications / "Old.app").symlink_to(external)
    with (applications / ".sciencemate-install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(installer, source, applications)
        assert result.returncode != 0
    result = run(installer, source, applications)
    assert result.returncode == 0, result.stderr
    assert external.exists() and (applications / "Old.app").is_symlink()


def test_packaged_shell_cli_uses_the_same_installation_engine(tmp_path):
    source = bundle(tmp_path / "download.app", "0.5.10")
    executable = source / "Contents/MacOS/ScienceMate"
    subprocess.run(["swiftc", "-framework", "AppKit", "-framework", "WebKit",
                    str(ROOT / "platform/desktop/mac/Shell.swift"),
                    str(ROOT / "platform/desktop/mac/Installation.swift"), "-o", str(executable)],
                   check=True, capture_output=True, text=True)
    applications = tmp_path / "Applications"
    result = subprocess.run([str(executable), "--install-to", str(applications)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    installed = applications / "ScienceMate.app"
    assert Path(result.stdout.splitlines()[-1]).resolve() == installed.resolve()
    assert (installed / "Contents/MacOS/ScienceMate").read_bytes() == executable.read_bytes()
    result = subprocess.run([str(executable), "--install-to", str(applications)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "already installed" in result.stdout
