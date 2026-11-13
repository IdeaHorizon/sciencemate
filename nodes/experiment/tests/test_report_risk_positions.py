import pytest
from nodes.experiment.tools import execution_guard as g
from shared.lib import dangerous_commands as shared


@pytest.mark.parametrize("cmd", [
    """printf '%s' 'no rm -rf used'""",
    """python3 -c 'import json; print(json.dumps({"note":"no rm -rf used"}))'""",
    """echo "no rm -rf used" >> report.log""",
])
def test_inert_report_uses_the_same_shared_and_node_classification(cmd):
    assert g.match_extra_high_risk(cmd) is None
    assert shared.match_high_risk(cmd, mode="shell") is None
    assert g.classify_high_risk(cmd) is None
    assert g.all_shell_risk_labels(cmd) == set()


@pytest.mark.parametrize("cmd", ["rm -rf disposable", "bash -c 'rm -rf disposable'",
                                  "echo $(rm -rf disposable)", "env rm -r disposable",
                                  "bash -c 'git push origin --force'", "sudo true"])
def test_executable_positions_keep_node_risk(cmd):
    assert g.match_extra_high_risk(cmd)
    assert g.all_shell_risk_labels(cmd)


def test_only_node_patterns_and_not_incomplete_parse_are_relaxed():
    assert g.match_extra_high_risk("echo 'no rm -rf used")  # malformed shell
    assert g.match_extra_high_risk("printf 'rm -rf disposable' | bash")
    assert g.match_extra_high_risk("print('shutil.rmtree( is source text')", mode="python") is None
    assert g.match_extra_high_risk("import shutil; shutil.rmtree('disposable')", mode="python")
    assert g.match_extra_high_risk("open('/dev/sda', 'w')", mode="python")
