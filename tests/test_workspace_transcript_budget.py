"""Large dependency trees must not interrupt transcript ingestion (#1343)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core import project_workspace
from core.state import State


def _paths(count: int) -> list[str]:
    return [f"experiments/{'目录' * 35}/{i}.py" for i in range(count)]


def test_many_file_headers_respect_the_total_patch_byte_budget():
    patch = "".join(
        f"diff --git a/{p} b/{p}\nnew file mode 100644\n"
        f"--- /dev/null\n+++ b/{p}\n@@ -0,0 +1 @@\n+small\n"
        for p in _paths(30000)
    )
    bounded, truncated = project_workspace._bounded_patch(patch)
    assert truncated
    assert len(bounded.encode("utf-8")) <= project_workspace._MAX_PATCH_BYTES


@pytest.mark.parametrize("event", ["workspace_changed", "workspace_checkpoint_requested"])
def test_final_workspace_jsonl_stays_within_the_reader_byte_limit(tmp_path: Path, event):
    state = State.new("experiment", tmp_path / "runtime")
    paths = _paths(30000)
    state.append_transcript(
        event, paths=paths, files_changed=len(paths), patch="", patch_truncated=False,
        file_stats=[], additions=3, deletions=1,
    )
    line = state.transcript_path.read_bytes().splitlines(keepends=True)[-1]
    assert len(line) <= 5_000_000
    record = json.loads(line)
    assert record["files_changed"] == 30000
    from core.workspace_transcript import restore_workspace_record
    restored = restore_workspace_record(record, state.transcript_path)
    assert restored["paths"] == paths
    assert restored["additions"] == 3 and restored["deletions"] == 1


@pytest.mark.parametrize("allowance", [0, 1, 7, 80, 400])
def test_a_single_long_non_ascii_header_and_marker_fit_the_allowance(allowance):
    section = ["diff --git a/" + "目录" * 1000, "+body"]
    bounded, cut = project_workspace._cut_section(section, allowance)
    assert cut
    assert len(bounded.encode("utf-8")) <= allowance


def test_small_workspace_records_keep_the_original_json_bytes(tmp_path):
    from core.workspace_transcript import encode_workspace_record, restore_workspace_record
    record = {"event": "workspace_changed", "paths": ["paper/结果.md"], "patch": "+结果\n"}
    line = encode_workspace_record(record, tmp_path / "transcript.jsonl")
    assert line == (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode()
    assert restore_workspace_record(record, tmp_path / "transcript.jsonl") is record
    assert not (tmp_path / "workspace-records").exists()


def _external_record(tmp_path, monkeypatch):
    from core import workspace_transcript as protocol
    monkeypatch.setattr(protocol, "MAX_TRANSCRIPT_RECORD_BYTES", 20000)
    transcript = tmp_path / "run" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    record = {"event": "workspace_changed", "paths": _paths(500), "file_stats": [], "patch": ""}
    compact = json.loads(protocol.encode_workspace_record(record, transcript))
    return protocol, transcript, record, compact


def test_every_external_page_has_a_byte_bound_and_preserves_all_fields(tmp_path, monkeypatch):
    protocol, transcript, record, compact = _external_record(tmp_path, monkeypatch)
    ref = compact["workspace_details"]
    pages = (transcript.parent / ref["path"]).read_bytes().splitlines(keepends=True)
    assert len(pages) == ref["pages"] > 1
    assert all(len(page) <= protocol.MAX_WORKSPACE_PAGE_BYTES for page in pages)
    restored = protocol.restore_workspace_record(compact, transcript)
    assert all(restored[key] == value for key, value in record.items())
    assert len(compact["paths"]) < len(record["paths"])


@pytest.mark.parametrize(
    "damage", ["missing", "missing_manifest", "hash", "count", "pages", "escape", "symlink",
               "oversized_page"],
)
def test_missing_tampered_or_escaped_details_are_explicit_errors(tmp_path, monkeypatch, damage):
    protocol, transcript, _, compact = _external_record(tmp_path, monkeypatch)
    ref = compact["workspace_details"]
    target = transcript.parent / ref["path"]
    if damage == "missing":
        target.unlink()
    elif damage == "missing_manifest":
        compact.pop("workspace_details")
    elif damage == "hash":
        data = target.read_bytes().replace(b"content", b"contenX", 1)
        target.write_bytes(data)
    elif damage == "count":
        ref["path_count"] += 1
    elif damage == "pages":
        ref["pages"] += 1
    elif damage == "escape":
        ref["path"] = "../" + ref["path"]
    elif damage == "symlink":
        outside = tmp_path / "outside.jsonl"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
    elif damage == "oversized_page":
        data = b" " * (protocol.MAX_WORKSPACE_PAGE_BYTES + 1) + b"\n"
        target.write_bytes(data)
        ref["storage_bytes"] = len(data)
        ref["storage_sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises((ValueError, KeyError)):
        protocol.restore_workspace_record(compact, transcript)


def test_large_unrelated_events_are_not_reinterpreted_as_workspace_details(tmp_path):
    from core.workspace_transcript import encode_workspace_record
    record = {"event": "tool_result", "content": "x" * 5_000_001}
    assert json.loads(encode_workspace_record(record, tmp_path / "transcript.jsonl")) == record
