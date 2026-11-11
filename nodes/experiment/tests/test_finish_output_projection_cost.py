"""Whole-tree verification must not reread observation sidecars per file.

The payload, readonly check, ledger, and finish verdict use the real node path.
Only the read-only projection is counted; no evidence or result is replaced.
"""

from nodes.experiment.tools import ledger
from nodes.experiment.tests.test_060_directory_member_lineage import (
    _close,
    _payload,
    directory_run,
)


def test_directory_delivery_projects_ledger_by_snapshot_not_member(directory_run, monkeypatch):
    _payload(directory_run,
             "p.mkdir(); [(p / f'{i}.txt').write_text('49\\n') for i in range(24)]")
    calls = []
    original = ledger.project_output_observations

    def measured(records):
        calls.append(len(records))
        return original(records)

    monkeypatch.setattr(ledger, "project_output_observations", measured)
    result = _close(directory_run, [f"{i}.txt" for i in range(24)])
    assert result.get("status") == "success", result
    # Before and after the real check are distinct snapshots. The number of
    # sidecar projections is independent of the 24 delivered file members.
    assert 2 <= len(calls) <= 4, calls
    assert max(calls) > min(calls), "readonly check facts must be reread"
