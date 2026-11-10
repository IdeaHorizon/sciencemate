"""Mac 壳从源码目录启动、再重启，也必须先进入安装版的引导入口。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("swiftc") is None,
                    reason="requires the macOS Swift toolchain")
def test_a_source_directory_cannot_shadow_the_installed_backend(tmp_path) -> None:
    shell = Path(__file__).resolve().parents[3] / "platform/desktop/mac/Shell.swift"
    # 跑实际 Backend 类，省掉窗口与 AppDelegate；用两个可区分的 app 包观察 Python 选中了谁。
    swift = tmp_path / "BackendProbe.swift"
    swift.write_text(shell.read_text(encoding="utf-8").split("// MARK: - 应用", 1)[0] + '''
let backend = try Backend(python: URL(fileURLWithPath: CommandLine.arguments[1]),
                          logDirectory: URL(fileURLWithPath: CommandLine.arguments[2]))
try backend.start()
_ = backend.waitUntilReady(timeout: 5)
print(backend.tailOfLog())
backend.stop()
''', encoding="utf-8")
    probe = tmp_path / "BackendProbe"
    compiled = subprocess.run(["swiftc", str(swift), "-o", str(probe)],
                              capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr

    bundled = tmp_path / "bundle/python"
    python = bundled / "bin/python3"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    installed = bundled / "site-packages"
    source = tmp_path / "source/platform/backend"
    for parent, marker in ((installed, "BUNDLED_BACKEND"), (source, "SOURCE_BACKEND")):
        app = parent / "app"
        app.mkdir(parents=True)
        (app / "__init__.py").write_text("", encoding="utf-8")
        (app / "launcher.py").write_text(f"print({marker!r})\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(installed)}
    for attempt in range(2):
        done = subprocess.run([str(probe), str(python), str(tmp_path / f"logs-{attempt}")],
                              cwd=source, env=env, capture_output=True, text=True, timeout=15)
        assert done.returncode == 0, done.stderr
        assert "BUNDLED_BACKEND" in done.stdout, done.stdout
        assert "SOURCE_BACKEND" not in done.stdout
