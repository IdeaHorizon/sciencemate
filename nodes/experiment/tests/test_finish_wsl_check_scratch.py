"""Finish checks export their scratch in the selected target's coordinates.

The executor is observed, not launched: physical WSL acceptance is a separate
developer-mode conversation. Core's real read-only TargetPaths and WSL mapping
are used with an already-observed synthetic mount prefix.
"""
from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath, PureWindowsPath
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core import execution_target, isolation
from core.isolation.wsl import WslBackend
from core.state import State
from nodes.experiment.tools import bash_paths, finish, safe_bash


class FinishScratchCoordinates(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hf-check-scratch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = State.new("experiment", self.root / "runs", project_id="scratch", session_id="owner")
        self.state.hook_state["node_inputs"] = {}
        self.calls = []
        self.ob = {"id": "report", "command": "printf CHECK_OK", "timeout_s": 120}

    def run_check(self, *, target="wsl:Ubuntu", scratch=None, mapper=None, evidence=False):
        scratch = scratch or PureWindowsPath(r"C:\Users\owner\work\ledger\check_scratch")
        readonly = [PureWindowsPath(r"C:\Users\owner\work\experiments")]

        async def executor(state, command, **kwargs):
            self.calls.append({"command": command, "kwargs": kwargs})
            return {"status": "success", "returncode": 0, "stdout_tail": "CHECK_OK", "stderr_tail": ""}

        def invoke():
            if not evidence:
                return asyncio.run(finish._default_check_runner(self.state, self.ob))

            class Ledger:
                def __init__(self):
                    self.rows = []

                def records(self):
                    return list(self.rows)

                def append(self, kind, **values):
                    self.rows.append({"seq": len(self.rows) + 1, "kind": kind, **values})

            return asyncio.run(finish._check_evidence(self.state, Ledger(), self.ob))

        with patch.object(execution_target, "_declared", execution_target.parse(target)), \
             patch.object(finish._obligations, "resolve_path", return_value=readonly[0]), \
             patch.object(finish, "read_only_roots", return_value=([scratch], readonly)), \
             patch.object(safe_bash, "_bash_static_validity_block", return_value=None), \
             patch.object(safe_bash, "_exec_and_log", executor), \
             patch.object(bash_paths, "_msys_absolute_path", side_effect=AssertionError("WSL must not use MSYS")):
            if mapper is None:
                backend = WslBackend("Ubuntu")
                backend._mount_prefix = "/observed-volumes/"
                backend._probed = True
                with patch.object(isolation, "_target_backend", return_value=backend):
                    result = invoke()
            else:
                with patch.object(isolation, "target_paths", **mapper):
                    result = invoke()
        return result, scratch, readonly

    def exported(self):
        self.assertEqual(len(self.calls), 1)
        prefix, command = self.calls[0]["command"].split("\n", 1)
        self.assertEqual(command, self.ob["command"])
        tokens = shlex.split(prefix)
        self.assertEqual(tokens[0], "export")
        return dict(token.split("=", 1) for token in tokens[1:])

    def test_real_wsl_mapping_is_used_for_all_three_temp_variables(self):
        result, scratch, readonly = self.run_check()
        self.assertEqual(self.exported(), {name: "/observed-volumes/c/Users/owner/work/ledger/check_scratch"
                                          for name in ("TMPDIR", "TMP", "TEMP")})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.calls[0]["kwargs"]["sandbox_roots"], ([scratch], readonly))
        self.assertTrue(self.calls[0]["kwargs"]["_read_only_check"])
        self.assertEqual(result["execution_profile"]["writable_roots"], [str(scratch)])
        self.assertEqual(result["execution_profile"]["read_only_roots"], [str(p) for p in readonly])

    def test_wsl_mapping_preserves_spaces_quotes_and_literal_shell_characters(self):
        scratch = PureWindowsPath("D:/Project O'Brien; literal/check scratch")
        self.run_check(scratch=scratch)
        expected = "/observed-volumes/d/Project O'Brien; literal/check scratch"
        self.assertEqual(self.exported(), {name: expected for name in ("TMPDIR", "TMP", "TEMP")})

    def test_native_windows_target_keeps_its_existing_host_coordinate(self):
        result, scratch, _ = self.run_check(
            target="local", mapper={"side_effect": AssertionError("local does not need WSL mapping")})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.exported(), {name: str(scratch) for name in ("TMPDIR", "TMP", "TEMP")})

    def test_native_posix_target_keeps_its_existing_host_coordinate(self):
        scratch = PurePosixPath("/var/tmp/normal check scratch")
        result, _, _ = self.run_check(target="local", scratch=scratch,
                                     mapper={"side_effect": AssertionError("local does not need WSL mapping")})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.exported(), {name: str(scratch) for name in ("TMPDIR", "TMP", "TEMP")})

    def test_unavailable_wsl_mapping_has_no_execution_or_host_fallback(self):
        class Mapping:
            def to_linux(self, path):
                raise isolation.IsolationContractError("selected distro unavailable")

        result, _, _ = self.run_check(mapper={"return_value": Mapping()})
        self.assertFalse(self.calls)
        self.assertIsNone(result["exit_code"])
        self.assertIs(result["executed"], False)
        self.assertIn("selected distro unavailable", result["error"])
        self.assertIn("WSL", result["error"])

    def test_core_mapping_factory_failure_is_retained_by_existing_check_evidence(self):
        # The shared risk analyzer also asks Core for target paths. If that
        # factory is unavailable first, the existing outer check boundary owns
        # the refusal; no host command may run and no success may be invented.
        result, _, _ = self.run_check(
            mapper={"side_effect": isolation.IsolationContractError("selected distro unavailable")},
            evidence=True)
        self.assertFalse(self.calls)
        self.assertFalse(result["passed"])
        self.assertIsNone(result["evidence"]["exit_code"])
        self.assertIn("selected distro unavailable", result["evidence"]["error"])

    def test_invalid_mapping_result_has_no_execution_or_host_fallback(self):
        for value in (None, "", "relative/scratch", r"C:\host\scratch"):
            with self.subTest(value=value):
                class Mapping:
                    def to_linux(self, path):
                        return value
                self.calls.clear()
                result, _, _ = self.run_check(mapper={"return_value": Mapping()})
                self.assertFalse(self.calls)
                self.assertIsNone(result["exit_code"])
                self.assertIs(result["executed"], False)


if __name__ == "__main__":
    unittest.main()
