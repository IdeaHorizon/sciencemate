"""A real verification may write scratch, never the evidence it verifies."""
from nodes.experiment.tests.test_060_directory_member_lineage import directory_run, _payload
from nodes.experiment.tests.test_060_finish_common_window import _command
from nodes.experiment.tools import finish


def test_check_scratch_is_discoverable_without_granting_evidence_write(directory_run):
    state, _, _, bundle, runner = directory_run
    _payload(directory_run, "p.mkdir(); (p / 'result.txt').write_text('49\\n')")
    command = _command(
        "import os; from pathlib import Path; "
        "assert os.environ['TMPDIR'] == os.environ['TMP'] == os.environ['TEMP']; "
        "Path(os.environ['TMPDIR'], 'recomputed.json').write_text('{\"ok\":true}'); "
        "assert Path('../bundle/result.txt').read_text() == '49\\n'; print('ALL_MATCH')"
    )
    result = runner.run(finish._default_check_runner(
        state, dict(command=command, cwd="runtime/child", timeout_s=20)))
    assert result["executed"] is True and result["exit_code"] == 0, result
    assert "ALL_MATCH" in result["stdout_tail"]
    assert (state.root / "ledger/check_scratch/recomputed.json").exists()
    attempted_mutation = runner.run(finish._default_check_runner(state, dict(
        command=_command("from pathlib import Path; Path('../bundle/result.txt').write_text('50\\n')"),
        cwd="runtime/child", timeout_s=20)))
    assert attempted_mutation["executed"] is True and attempted_mutation["exit_code"] != 0
    assert (bundle / "result.txt").read_text() == "49\n"
