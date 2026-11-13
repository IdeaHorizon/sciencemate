"""Quoted report data must not be classified as an executed deletion (#1384)."""

import pytest

from shared.lib import dangerous_commands as danger


@pytest.mark.parametrize(
    "command",
    [
        "printf '%s' 'no rm -rf used'",
        "echo 'sudo rm -rf old; DROP TABLE results' > report.txt",
        "cat <<'REPORT'\nno rm -rf used\nREPORT",
        "python -c \"import json; json.dump({'note': 'no rm -rf used'}, "
        "open('report.json', 'w'))\"",
        "bash -c \"printf '%s' 'no rm -rf used'\"",
        "printf '%s' 'no rm -rf used' > report.txt; sha256sum report.txt",
        "python - <<'PY'\nimport json\njson.dump({'note': 'rm -rf'}, open('report.json', 'w'))\nPY",
    ],
)
def test_report_data_does_not_request_execution_confirmation(command):
    assert danger.match_high_risk(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf old",
        "bash -c 'rm -rf old'",
        "printf '%s' \"$(rm -rf old)\"",
        "printf '%s' 'rm -rf old' | bash",
        "eval 'rm -rf old'",
        "printf text > /dev/sda",
        "python -c \"import os; os.system('rm -rf old')\"",
        "'rm' -rf old",
        "bash <<'SCRIPT'\nrm -rf old\nSCRIPT",
        "printf '%s' 'rm -rf old' | xargs sh -c",
        "payload='rm -rf old'; eval \"$payload\"",
        "printf '%s' 'rm -rf old' > script.sh; bash script.sh",
        "payload='rm -rf old'; bash -c 'eval \"$payload\"'",
        "printf '%s' 'rm -rf old' | opaque_consumer",
        "/tmp/printf '%s' 'rm -rf old'",
        "/tmp/python -c \"print('rm -rf old')\"",
    ],
)
def test_executing_and_unknown_consumers_retain_risk_classification(command):
    assert danger.match_high_risk(command) is not None


@pytest.mark.parametrize(
    "source",
    [
        "print('shutil.rmtree( is only report text')",
        "import json; json.dump({'note': 'os.system( is source text'}, open('report.json', 'w'))",
    ],
)
def test_python_report_literals_are_data(source):
    assert danger.match_high_risk(source, mode="python") is None


@pytest.mark.parametrize(
    "source",
    [
        "import shutil; shutil.rmtree('old')",
        "import os; os.system('rm -rf old')",
        "exec('import shutil; shutil.rmtree(\"old\")')",
    ],
)
def test_executed_or_unknown_python_retains_classification(source):
    assert danger.match_high_risk(source, mode="python") is not None
