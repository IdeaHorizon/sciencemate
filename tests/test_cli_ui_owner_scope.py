"""nidy can maintain the complete CLI UI while shared execution stays protected."""

import fnmatch
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_nidy_can_change_ui_docs_and_tests_but_not_runtime():
    scopes = yaml.safe_load((ROOT / ".scope_map.yaml").read_text(encoding="utf-8"))
    allowed = scopes["nidy"] + scopes.get("_anyone", [])

    def permits(path):
        return any(fnmatch.fnmatch(path, pattern) for pattern in allowed)

    for path in [
        "cli/chat.py",
        "cli/terminal.py",
        "cli/input.py",
        "cli/commands.py",
        "cli/README.md",
        "cli/tests/test_frontend.py",
        "nodes/analysis/harness.yaml",
    ]:
        assert permits(path), path
    for path in [
        "chat.py",
        "core/session_driver.py",
        "platform_runtime.py",
        "platform/backend/app/services/local_execution.py",
        ".scope_map.yaml",
        "framework_exemptions.yaml",
    ]:
        assert not permits(path), path


def test_cli_review_owner_is_nidy():
    owners = (ROOT / ".gitea/CODEOWNERS").read_text(encoding="utf-8")
    assert any(line.split() == ["/cli/", "@nidy"] for line in owners.splitlines())
