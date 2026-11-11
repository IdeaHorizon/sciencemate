"""Run directly with the bundled Windows Python; no pytest dependency required."""

import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@unittest.skipUnless(sys.platform == "win32", "requires Windows native execution")
class WindowsBacklogContracts(unittest.TestCase):
    def setUp(self):
        from shared.lib.filesystem import io_path

        self.base = Path(tempfile.mkdtemp(prefix="hf-backlog-native-"))
        self.addCleanup(lambda: shutil.rmtree(io_path(self.base)))

    def test_long_default_cwd_and_exact_write_receipt(self):
        from core.project_workspace import resolve_tool_path, working_directory
        from shared.lib.filesystem import io_path
        from shared.tools.builtin import _write_file

        project = self.base / "project"
        while len(str(project)) < 135:
            project /= "p" * 20
        notes = project / "notes"
        notes.mkdir(parents=True)
        run = project / ".research/runtime/runs" / ("orchestrator_" + "x" * 95)
        io_path(run / "scratch").mkdir(parents=True)
        state = SimpleNamespace(
            root=run,
            node_type="_orchestrator",
            project_worktree=project,
            workspace_root=notes,
            workspace_relative_path="notes",
            hook_state={},
            files_read=set(),
        )
        self.assertGreaterEqual(len(str(run / "scratch")), 260)
        with self.assertRaises(OSError):
            subprocess.run(
                [sys.executable, "-I", "-c", 'print("control")'],
                cwd=run / "scratch",
                check=True,
                capture_output=True,
            )
        cwd = working_directory(state)
        self.assertLess(len(str(cwd)), 248)
        result = subprocess.run(
            [sys.executable, "-I", "-c", "import os; print(os.getcwd())"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertTrue(result.stdout.strip())
        for index, content in enumerate(["a\nb\n", "a\r\nb\r\n", "试验\n"]):
            receipt = asyncio.run(_write_file(state, f"result-{index}.txt", content))
            self.assertEqual(receipt["status"], "success", receipt)
            path = resolve_tool_path(state, f"result-{index}.txt")
            self.assertEqual(path.read_bytes(), content.encode("utf-8"))
            self.assertEqual(receipt["bytes_written"], len(path.read_bytes()))

    def test_selected_wsl_maps_the_same_real_project_in_both_directions(self):
        from core import execution_target, isolation, project_workspace
        from core.isolation.wsl import list_distros

        distros = [name for name in list_distros() if not name.startswith("docker-")]
        if not distros:
            self.skipTest("no WSL distribution installed")
        project = self.base / "project"
        notes = project / "notes"
        notes.mkdir(parents=True)
        state = SimpleNamespace(
            root=self.base / "run",
            node_type="_orchestrator",
            project_worktree=project,
            workspace_root=notes,
            workspace_relative_path="notes",
            hook_state={},
        )
        execution_target.declare("wsl:" + distros[0])
        self.addCleanup(execution_target._reset_for_tests)
        self.addCleanup(isolation._reset_for_tests)
        mapper = isolation.target_paths()
        guest = mapper.to_linux(str(notes))
        self.assertEqual(Path(mapper.to_windows(guest)).resolve(), notes.resolve())
        self.assertEqual(project_workspace.validate_tool_cwd(state, guest), notes.resolve())
        self.assertIsNone(mapper.to_windows("/home/no-host-mapping"))
        self.assertFalse(hasattr(mapper, "prepare"))

    def test_same_root_can_become_readonly_without_revoking_nested_scratch(self):
        from core.isolation import CommandSpec
        from core.isolation.win32 import Win32Backend

        business, scratch = self.base / "business", self.base / "check_scratch"
        business.mkdir()
        scratch.mkdir()
        marker = business / "evidence.txt"
        marker.write_text("before")
        state = SimpleNamespace()
        backend = Win32Backend()

        def launch(code, writable, readonly):
            prepared = backend.prepare(
                CommandSpec(
                    argv=[sys.executable, "-I", "-c", code],
                    cwd=str(self.base),
                    writable_roots=writable,
                    readonly_roots=readonly,
                ),
                state=state,
            )
            try:
                return subprocess.run(
                    prepared.argv,
                    cwd=prepared.cwd,
                    env=prepared.env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            finally:
                prepared.cleanup()

        first = launch(
            f'from pathlib import Path; Path({str(marker)!r}).write_text("produced")',
            [business],
            [self.base],
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        before = marker.stat()
        code = f"""
from pathlib import Path
target = Path({str(marker)!r})
blocked = []
for operation in [lambda: target.write_text('wrong'), lambda: target.unlink()]:
    try:
        operation()
    except PermissionError:
        blocked.append(True)
    else:
        raise AssertionError('read-only evidence was changed')
Path({str(scratch / "ok")!r}).write_text('scratch works')
assert len(blocked) == 2
"""
        second = launch(code, [scratch], [self.base, business])
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(marker.read_text(), "produced")
        self.assertEqual(marker.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual((scratch / "ok").read_text(), "scratch works")

    def test_competing_label_changes_wait_and_an_abandoned_owner_releases_them(self):
        from core.isolation import CommandSpec
        from core.isolation.win32 import Win32Backend

        backend = Win32Backend()
        for abandoned in (False, True):
            with self.subTest(abandoned=abandoned):
                root = self.base / str(abandoned)
                business, scratch = root / "business", root / "scratch"
                business.mkdir(parents=True)
                scratch.mkdir()
                marker, ready = business / "evidence", scratch / "ready"
                marker.write_text("before")
                first_code = (
                    "from pathlib import Path; import time; "
                    f"Path({str(ready)!r}).write_text('ready'); "
                    f"time.sleep({60 if abandoned else 0.5});\n"
                    f"try: Path({str(marker)!r}).write_text('wrong')\n"
                    "except PermissionError: pass\n"
                    "else: raise AssertionError('concurrent relabel revoked read-only protection')"
                )
                second_code = f"from pathlib import Path; Path({str(marker)!r}).write_text('later')"
                prepared = [
                    backend.prepare(
                        CommandSpec(
                            argv=[sys.executable, "-I", "-c", code],
                            cwd=str(root),
                            writable_roots=rw,
                            readonly_roots=[root, *ro],
                        ),
                        state=SimpleNamespace(),
                    )
                    for code, rw, ro in [
                        (first_code, [scratch], [business]),
                        (second_code, [business], []),
                    ]
                ]
                processes = []
                try:
                    first = subprocess.Popen(
                        prepared[0].argv,
                        cwd=prepared[0].cwd,
                        env=prepared[0].env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    processes.append(first)
                    deadline = time.monotonic() + 10
                    while not ready.exists() and time.monotonic() < deadline:
                        if first.poll() is not None:
                            break
                        time.sleep(0.01)
                    self.assertTrue(
                        ready.exists(), first.communicate() if first.poll() is not None else ""
                    )
                    second = subprocess.Popen(
                        prepared[1].argv,
                        cwd=prepared[1].cwd,
                        env=prepared[1].env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    processes.append(second)
                    if abandoned:
                        first.terminate()
                    first_out = first.communicate(timeout=10)
                    second_out = second.communicate(timeout=10)
                    if not abandoned:
                        self.assertEqual(first.returncode, 0, first_out)
                    self.assertEqual(second.returncode, 0, second_out)
                    self.assertEqual(marker.read_text(), "later")
                finally:
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                        process.communicate()
                    for launch in prepared:
                        launch.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
