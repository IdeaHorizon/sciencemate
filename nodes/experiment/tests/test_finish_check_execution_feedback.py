"""Executor feedback must survive read-only finish-check translation."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.state import State
from nodes.experiment.tools import finish, safe_bash, timeout_escalation


class Ledger:
    def __init__(self):
        self.rows = []

    def records(self):
        return list(self.rows)

    def append(self, kind, **values):
        self.rows.append({"seq": len(self.rows) + 1, "kind": kind, **values})


class CheckExecutionFeedback(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hf-check-feedback-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = State.new("experiment", self.root / "runs", project_id="check-feedback", session_id="owner")
        self.state.hook_state["node_inputs"] = {}
        self.ob = {"id": "probe", "kind": "check", "command": "echo CHECK_OK", "expect_exit": 0,
                   "expect_stdout_contains": "CHECK_OK", "cwd": str(self.root), "timeout_s": 120}
        self.calls = []

    def check(self, result=None, error=None, ob=None):
        async def executor(*args, **kwargs):
            self.calls.append((args, kwargs))
            if error is not None:
                raise error
            return copy.deepcopy(result)

        ledger = Ledger()
        with patch.object(safe_bash, "_exec_and_log", executor), \
             patch.object(finish._obligations, "resolve_path", lambda state, value: self.root), \
             patch.object(finish, "read_only_roots", lambda state, cwd: ([self.root / "scratch"], [self.root])):
            value = asyncio.run(finish._check_evidence(self.state, ledger, ob or self.ob))
        return value, ledger.rows

    def test_original_timeout_is_explained_without_a_fabricated_exit(self):
        physical = timeout_escalation.build_timeout_payload(
            self.state, tool="safe_run_bash", cmd=self.ob["command"], timeout_s=120)
        value, rows = self.check(physical)
        evidence = value["evidence"]
        self.assertFalse(value["passed"])
        self.assertEqual(evidence["execution_status"], "timeout")
        self.assertEqual(evidence["timeout_s"], 120)
        self.assertIn("120", evidence["error"])
        self.assertIsNone(evidence["exit_code"])
        self.assertIsNone(evidence["executed"])
        self.assertIsNone(rows[-1]["exit_code"])
        self.assertIsNone(rows[-1]["executed"])
        self.assertIn("120", rows[-1]["error"])

    def test_timeout_variant_retains_partial_output_and_explicit_spawn_fact(self):
        physical = timeout_escalation.build_timeout_payload(
            self.state, tool="safe_run_bash", cmd="python verify.py", timeout_s=180,
            stdout="loaded dataset\n", stderr="still checking\n")
        physical["payload_spawned"] = True
        value, _ = self.check(physical)
        evidence = value["evidence"]
        self.assertFalse(value["passed"])
        self.assertEqual(evidence["timeout_s"], 180)
        self.assertEqual(evidence["stdout_tail"], "loaded dataset\n")
        self.assertEqual(evidence["stderr_tail"], "still checking\n")
        self.assertIs(evidence["executed"], True)
        self.assertIsNone(evidence["exit_code"])

    def test_explicit_no_spawn_is_not_promoted_to_execution(self):
        value, _ = self.check({"status": "timeout", "timeout_s": 3, "payload_spawned": False})
        self.assertIs(value["evidence"]["executed"], False)
        self.assertFalse(value["passed"])

    def test_normal_success_still_passes(self):
        value, _ = self.check({"status": "success", "returncode": 0, "stdout_tail": "CHECK_OK\n", "stderr_tail": ""})
        self.assertTrue(value["passed"])
        self.assertEqual(value["evidence"]["exit_code"], 0)
        self.assertIs(value["evidence"]["executed"], True)

    def test_nonzero_exit_is_not_replaced_with_timeout_or_success(self):
        value, _ = self.check({"status": "error", "returncode": 7, "stdout_tail": "CHECK_OK\n", "stderr_tail": "invalid input"})
        self.assertFalse(value["passed"])
        self.assertEqual(value["evidence"]["exit_code"], 7)
        self.assertIs(value["evidence"]["executed"], True)
        self.assertNotEqual(value["evidence"].get("execution_status"), "timeout")

    def test_high_risk_check_still_refuses_before_executor(self):
        ob = {**self.ob, "command": "kill -9 $$"}
        value, _ = self.check({"status": "success", "returncode": 0}, ob=ob)
        self.assertFalse(value["passed"])
        self.assertIs(value["evidence"]["executed"], False)
        self.assertTrue(value["evidence"]["risk_category"])
        self.assertEqual(self.calls, [])

    def test_executor_exception_is_preserved(self):
        value, rows = self.check(error=RuntimeError("executor disconnected"))
        self.assertFalse(value["passed"])
        self.assertIn("executor disconnected", value["evidence"]["error"])
        self.assertIsNone(value["evidence"]["exit_code"])
        self.assertIsNone(value["evidence"]["executed"])
        self.assertIsNone(rows[-1]["executed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
