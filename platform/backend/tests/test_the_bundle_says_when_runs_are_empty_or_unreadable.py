"""诊断包：runs/ 一个文件都没有时要说清是空的还是读不到（RFC_WORKER_START_IS_NOT_A_GATE §1.3，窄单 E）。

lujy 10-15 的包：README 承诺「runs/ 这个会话的全部运行记录」，包里零个 run，`skipped` 也是空的 ——
看包的人分不出「worker 在建 run 目录之前就死了」和「收包的时候读不到」。
`os.walk` 默认把进不去的目录静默跳过，所以「读不到」和「本来就没有」在包里长得一样。
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

from app.services.session_diagnostics import write_diagnostics_bundle


def _bundle(tmp_path: Path, runs_root: Path) -> dict:
    bundle = write_diagnostics_bundle(
        project_id="p", session_id="s", session_facts={}, session_created_at=None,
        runs_root=runs_root, logs_dir=tmp_path / "no-logs", secrets=(),
        shared_log=False, profile_label="personal", app_version=None, destination_dir=tmp_path,
    )
    try:
        data = bundle.path.read_bytes()
    finally:
        bundle.path.unlink()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
    return {"names": names, "manifest": manifest}


def test_an_empty_runs_root_is_named_as_empty_not_silently_absent(tmp_path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    got = _bundle(tmp_path, runs)
    assert not [n for n in got["names"] if n.startswith("runs/")]
    reasons = {entry["path"]: entry["reason"] for entry in got["manifest"]["skipped"]}
    assert "runs/" in reasons
    assert "空" in reasons["runs/"] and "没有任何 run" in reasons["runs/"]


def test_a_missing_runs_root_still_says_so(tmp_path) -> None:
    got = _bundle(tmp_path, tmp_path / "never-made")
    reasons = {entry["path"]: entry["reason"] for entry in got["manifest"]["skipped"]}
    assert "没有运行记录目录" in reasons["runs/"]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="chmod 对 root / Windows 不生效")
def test_an_unreadable_run_dir_is_named_in_skipped_not_dropped(tmp_path) -> None:
    runs = tmp_path / "runs"
    good = runs / "orchestrator__p__session__s"
    good.mkdir(parents=True)
    (good / "events.jsonl").write_text('{"type":"x"}\n', encoding="utf-8")
    locked = runs / "literature__child"
    locked.mkdir()
    (locked / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    locked.chmod(0)
    try:
        got = _bundle(tmp_path, runs)
    finally:
        locked.chmod(0o700)
    assert "runs/orchestrator__p__session__s/events.jsonl" in got["names"]
    skipped = {entry["path"]: entry["reason"] for entry in got["manifest"]["skipped"]}
    assert "runs/literature__child/" in skipped
    assert "读不到" in skipped["runs/literature__child/"]
    assert "Permission" in skipped["runs/literature__child/"] or "拒绝" in skipped["runs/literature__child/"]
    assert "runs/" not in skipped, "有东西收进去了，就不该再说目录是空的"
