"""060 S1a：账本、capture observation、obligations 的事实语义。

每格对应 governing document 的一条不变量；没有一格是"多拒一种情况"。
"""
from __future__ import annotations

import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import capture, ledger, obligations
from nodes.experiment.tools.protocol_epoch import LEDGER_SCHEMA_VERSION, PROTOCOL_EPOCH


def _fresh(tmp_path: Path, session: str = "s-1") -> State:
    return State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id=session)


def _fd_kind(fd: int) -> str:
    return "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"


# ── 账本 ──────────────────────────────────────────────────────────────────────

def test_every_record_carries_the_epoch_markers_and_seq_is_monotonic(tmp_path):
    state = _fresh(tmp_path)
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    assert not led.read_only and led.epoch == PROTOCOL_EPOCH
    a = led.append("note", text="one")
    b = led.append("note", text="two")
    assert (a["seq"], b["seq"]) == (1, 2)
    rows = led.records()
    assert [r["kind"] for r in rows] == ["note", "note"]
    assert all(r["protocol_epoch"] == PROTOCOL_EPOCH and r["schema_version"] == LEDGER_SCHEMA_VERSION for r in rows)
    meta = json.loads((ledger.ledger_dir(state) / "ledger.meta.json").read_text())
    assert meta["run_id"] == state.run_id and meta["protocol_epoch"] == PROTOCOL_EPOCH

def test_append_once_replays_identical_material_and_rejects_reinterpretation(tmp_path):
    state = _fresh(tmp_path)
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    first = led.append_once("finish", "finish:k", outcome="failed", closure_id="c")
    replay = led.append_once("finish", "finish:k", outcome="failed", closure_id="c")
    assert replay == first
    assert [row["kind"] for row in led.records()] == ["finish"]
    with pytest.raises(ledger.LedgerCorrupt, match="idempotency conflict"):
        led.append_once("finish", "finish:k", outcome="success", closure_id="c")


def test_append_once_reconfirms_visible_row_after_repeated_file_fsync_failure(
    tmp_path, monkeypatch,
):
    state = _fresh(tmp_path)
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    real_fsync = ledger.os.fsync
    calls: list[str] = []
    remaining_file_failures = 2

    def injected_fsync(fd):
        nonlocal remaining_file_failures
        kind = _fd_kind(fd)
        calls.append(kind)
        if kind == "file" and remaining_file_failures:
            remaining_file_failures -= 1
            raise OSError("injected event file fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(ledger.os, "fsync", injected_fsync)
    for _attempt in range(2):
        with pytest.raises(OSError, match="event file fsync failure"):
            led.append_once(
                "finish", "finish:durability",
                outcome="failed", closure_id="c",
            )
        assert len(led.records()) == 1

    replay = led.append_once(
        "finish", "finish:durability",
        outcome="failed", closure_id="c",
    )
    assert replay["idempotency_key"] == "finish:durability"
    assert calls == ["file", "file", "file", "directory"]
    assert len(led.records()) == 1


def test_append_once_retries_a_failed_first_creation_directory_fsync(
    tmp_path, monkeypatch,
):
    state = _fresh(tmp_path)
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    real_fsync = ledger.os.fsync
    calls: list[str] = []
    fail_directory_once = True

    def injected_fsync(fd):
        nonlocal fail_directory_once
        kind = _fd_kind(fd)
        calls.append(kind)
        if kind == "directory" and fail_directory_once:
            fail_directory_once = False
            raise OSError("injected event directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(ledger.os, "fsync", injected_fsync)
    with pytest.raises(OSError, match="event directory fsync failure"):
        led.append_once(
            "finish", "finish:directory",
            outcome="failed", closure_id="c",
        )
    assert len(led.records()) == 1

    replay = led.append_once(
        "finish", "finish:directory",
        outcome="failed", closure_id="c",
    )
    assert replay["idempotency_key"] == "finish:directory"
    assert calls == ["file", "directory", "file", "directory"]
    assert len(led.records()) == 1


def test_append_once_material_comparison_distinguishes_bool_from_int(tmp_path):
    state = _fresh(tmp_path)
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    cases = [
        (True, 1),
        (1, True),
        ({"nested": [False]}, {"nested": [0]}),
    ]
    for index, (first, conflicting) in enumerate(cases):
        key = f"finish:typed:{index}"
        led.append_once("finish", key, material=first)
        with pytest.raises(ledger.LedgerCorrupt, match="idempotency conflict"):
            led.append_once("finish", key, material=conflicting)

    array = led.append_once(
        "finish", "finish:json-array", material=("same", 1),
    )
    replay = led.append_once(
        "finish", "finish:json-array", material=["same", 1],
    )
    assert replay["seq"] == array["seq"]
    assert replay["material"] == ["same", 1]
    assert len(led.records()) == len(cases) + 1


def test_two_same_owner_ledger_instances_allocate_unique_monotonic_sequences(tmp_path):
    state = _fresh(tmp_path)
    first = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    second = ledger.Ledger(root=first.root, epoch=first.epoch, read_only=False,
                           owner=first.owner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda item: item[0].append("note", text=item[1]),
                             [(first, "a"), (second, "b")]))
    assert sorted(row["seq"] for row in rows) == [1, 2]
    persisted = first.records()
    assert [row["seq"] for row in persisted] == [1, 2]
    assert {row["text"] for row in persisted} == {"a", "b"}


def test_concurrent_identical_append_once_has_one_physical_row(tmp_path):
    state = _fresh(tmp_path)
    first = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    second = ledger.Ledger(root=first.root, epoch=first.epoch, read_only=False,
                           owner=first.owner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda led: led.append_once(
            "finish", "finish:shared", outcome="blocked", closure_id="c"),
            (first, second)))
    assert rows[0] == rows[1]
    assert len(first.records()) == 1


def test_a_half_written_last_line_is_reported_and_ignored_but_a_broken_middle_line_is_fatal(tmp_path):
    state = _fresh(tmp_path)
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    led.append("note", text="ok")
    events = ledger.ledger_dir(state) / "events.jsonl"
    with events.open("ab") as fh:
        fh.write(b'{"seq": 2, "kind": "note", "protocol_epoch": "v3", "sch')  # 写到一半死掉
    # 只读打开：如实报告坏尾，忽略它，不改文件
    ro = ledger.open_ledger(state)
    assert [r["text"] for r in ro.records()] == ["ok"]
    assert ro.tail_report()["corrupt_tail_bytes"] > 0
    # owner 接手：截掉坏尾并记一条恢复记录，之后的写入可读
    reopened = ledger.open_ledger(state, write=True, bootstrap=True)
    kinds = [r["kind"] for r in reopened.records()]
    assert kinds == ["note", "corrupt_tail_recovered"]
    assert reopened.records()[-1]["bytes"] > 0 and reopened.tail_report()["corrupt_tail_bytes"] == 0
    reopened.append("note", text="after")
    assert [r["text"] for r in reopened.records() if r["kind"] == "note"] == ["ok", "after"]
    # 中间坏行：fail-loud，不静默跳过
    with events.open("ab") as fh:
        fh.write(b"not json at all\n")
    with pytest.raises(ledger.LedgerCorrupt):
        ledger.open_ledger(state).records()


def test_only_the_owner_writes_and_a_live_foreign_session_reads_only(tmp_path):
    state = _fresh(tmp_path, session="owner")
    ledger.open_ledger(state, write=True, bootstrap=True).append("note", text="mine")
    other = State.reopen(run_id=state.run_id, node_type="experiment", base_dir=tmp_path / "runs",
                         project_id="p", session_id="intruder")
    led = ledger.open_ledger(other, write=True)
    assert led.read_only and "owned by live session" in led.reason
    with pytest.raises(ledger.LedgerReadOnly):
        led.append("note", text="theirs")
    assert [r["text"] for r in ledger.open_ledger(other).records()] == ["mine"]


def test_reattach_after_the_owner_process_died_needs_the_explicit_flag(tmp_path):
    state = _fresh(tmp_path, session="owner")
    ledger.open_ledger(state, write=True, bootstrap=True).append("note", text="before crash")
    lock = ledger.ledger_dir(state) / "owner.lock"
    payload = json.loads(lock.read_text())
    payload["pid"] = 2_000_000_000  # 一个不存在的 pid：owner 进程死了
    lock.write_text(json.dumps(payload))
    human = State.reopen(run_id=state.run_id, node_type="experiment", base_dir=tmp_path / "runs",
                         project_id="p", session_id="human-reopen")
    assert ledger.open_ledger(human, write=True).read_only
    taken = ledger.open_ledger(human, write=True, reattach=True)
    assert not taken.read_only
    taken.append("note", text="after reattach")
    assert [r["text"] for r in taken.records()] == ["before crash", "after reattach"]


def test_an_old_protocol_run_is_read_only_and_a_transcript_alone_does_not_make_one(tmp_path):
    # 新协议的 run 也有 transcript 与（过渡期）v2 的验收锁：它们不能当旧协议标记
    state = _fresh(tmp_path)
    (Path(state.root) / "transcript.jsonl").write_text('{"kind": "anything"}\n')
    (Path(state.root) / ".run_acceptance_receipt.lock").write_text("")
    assert ledger.detect_epoch(state) == "fresh"
    # 旧协议独有的落盘痕迹（v2 收尾的 operation_evidence 目录）→ old_protocol，只读，不建 meta
    old = _fresh(tmp_path / "old")
    (Path(old.root) / "operation_evidence").mkdir()
    assert ledger.detect_epoch(old) == "old_protocol"
    led = ledger.open_ledger(old, write=True)
    assert led.read_only and "old_protocol" in led.reason
    assert not (ledger.ledger_dir(old) / "ledger.meta.json").exists()
    # v2 收尾件（owner 是 record_operation_completion 的 artifact）同样算
    closed = _fresh(tmp_path / "closed")
    closed.save_artifact("experiment_log", "old_log", "# old", metadata={
        "operation_closure_owner": "record_operation_completion", "operation_closure_id": "x:operation"})
    assert ledger.detect_epoch(closed) == "old_protocol"


def test_job_projection_follows_intent_bound_terminal_and_unknown(tmp_path):
    state = _fresh(tmp_path)
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    led.append("intent", nonce="n1", scheduler="slurm", output_root="/r/1")
    led.append("job_bound", nonce="n1", external_id="123")
    led.append("intent", nonce="n2", scheduler="slurm", output_root="/r/2")
    led.append("job_unknown", nonce="n2", reason="ack lost; adapter cannot query")
    led.append("job_terminal", nonce="n1", derived_state="COMPLETED", exit_code="0:0", raw="123|COMPLETED|0:0")
    jobs = led.project_jobs()
    assert jobs["n1"]["state"] == "terminal" and jobs["n1"]["external_id"] == "123"
    assert jobs["n2"]["state"] == "unknown"
    assert [j["nonce"] for j in ledger.open_jobs(led.records())] == ["n2"]


# ── capture / observation ─────────────────────────────────────────────────────

def test_observation_tells_created_modified_in_place_and_pre_existing_apart(tmp_path):
    root = tmp_path / "work"; root.mkdir()
    pre = root / "pre.bin"; pre.write_bytes(b"x" * 16)
    rewrite = root / "same_size.bin"; rewrite.write_bytes(b"a" * 16)
    before = capture.snapshot([root / "new.bin"], [root])
    time.sleep(0.01)
    (root / "new.bin").write_bytes(b"new")
    os.utime(pre, ns=(pre.stat().st_atime_ns, pre.stat().st_mtime_ns))  # 未变
    rewrite.write_bytes(b"b" * 16)  # 同 inode 同大小的原地改写
    after = capture.snapshot([root / "new.bin"], [root])
    d = capture.diff(before, after)
    assert str(root / "new.bin") in d["created"]
    assert str(rewrite) in d["modified"] and d["identity_basis"][str(rewrite)] == "sha256"
    assert str(pre) not in d["modified"] and str(pre) not in d["created"]
    assert capture.written_in_action(d, root / "new.bin") == "created"
    assert capture.written_in_action(d, pre) == "unchanged"


def test_large_file_without_hash_uses_size_inode_mtime_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "SMALL_FILE_BYTES", 4)
    root = tmp_path / "w"; root.mkdir()
    big = root / "big.bin"; big.write_bytes(b"0123456789")
    before = capture.snapshot([big])
    time.sleep(0.01)
    big.write_bytes(b"9876543210")  # 同大小、同 inode、新 mtime
    after = capture.snapshot([big])
    d = capture.diff(before, after)
    assert str(big) in d["modified"] and d["identity_basis"][str(big)] == "size+inode+mtime"
    assert "sha256" not in after["entries"][str(big)]


def test_snapshot_budget_truncates_honestly(tmp_path):
    root = tmp_path / "many"; root.mkdir()
    for i in range(50):
        (root / f"f{i}").write_text("x")
    snap = capture.snapshot([], [root], max_files=10)
    assert snap["truncated"] is True and snap["scanned"] == 10


def test_the_capture_record_dir_is_idempotent_and_does_not_claim_to_isolate_output(tmp_path):
    state = _fresh(tmp_path)
    a = capture.allocate_record_dir(state, "0001-bash")
    b = capture.allocate_record_dir(state, "0001-bash")
    assert a == b and a.is_dir() and str(a).startswith(str(Path(state.root)))
    # 它存的是这次 action 的记录，不是 payload 的输出根：本地命令仍在 cwd 里跑。
    assert "输出根" in capture.allocate_record_dir.__doc__ and "不是" in capture.allocate_record_dir.__doc__


# ── obligations ──────────────────────────────────────────────────────────────

def test_obligations_derive_from_structured_inputs_and_always_include_the_three_artifacts(tmp_path):
    state = _fresh(tmp_path)
    state.hook_state["node_inputs"] = {"required_outputs": [
        {"id": "exe", "kind": "file", "path": "build/bin/hello"},
        {"id": "smoke", "kind": "check", "command": "build/bin/hello", "expect_stdout_contains": "OK"},
    ]}
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    out = obligations.freeze(state)
    assert out["status"] == "frozen"
    ids = [o["id"] for o in out["obligations"]]
    assert ids[:2] == ["exe", "smoke"] and ids[-3:] == ["raw_results", "clean_results", "experiment_log"]
    assert all(o["required"] for o in out["obligations"])
    assert obligations.freeze(state)["status"] == "already_frozen"
    assert [r["kind"] for r in ledger.open_ledger(state).records()] == ["obligations_frozen"]


def test_free_text_task_freezes_its_source_and_cannot_be_replaced_by_supplied_checks(tmp_path):
    state = _fresh(tmp_path)
    state.hook_state["node_inputs"] = {"experiment_spec": "自由文本任务：构建并运行 smoke"}
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    first = obligations.freeze(state)
    assert first["status"] == "frozen"
    original = first["obligations"]
    assert original[0]["kind"] == "task"
    assert original[0]["text"] == "自由文本任务：构建并运行 smoke"
    second = obligations.freeze(state, supplied=[{"kind": "file", "path": "/tmp/x/hello"}])
    assert second["status"] == "already_frozen" and second["obligations"] == original
    # 后验检查与实际交付不能被当成新的 A
    third = obligations.freeze(state, supplied=[{"kind": "file", "path": "/tmp/other"}])
    assert third["status"] == "already_frozen" and third["obligations"] == original
    assert obligations.task_source(state) == {
        "source": "node_inputs.experiment_spec", "text": "自由文本任务：构建并运行 smoke"}


def test_required_items_are_immutable_but_optional_findings_may_be_added(tmp_path):
    state = _fresh(tmp_path)
    state.hook_state["node_inputs"] = {"required_outputs": [{"id": "exe", "path": "bin/hello"}]}
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    obligations.freeze(state)
    added = obligations.add_optional(state, [{"id": "profile", "path": "bin/profile.txt"}])
    assert added["ids"] == ["profile"]
    rows = {o["id"]: o for o in obligations.frozen(state)}
    assert rows["exe"]["required"] is True and rows["profile"]["required"] is False
    with pytest.raises(obligations.ObligationError):
        obligations.add_optional(state, [{"id": "exe", "path": "elsewhere"}])


def test_a_record_may_carry_a_field_called_state_or_kind(tmp_path):
    """记录里合法的字段名包括 state / kind / seq / ts。

    活体证据（第三次方法预览，2026-09-23）：``try_record(state, "action_settled",
    state="settled", ...)`` 把状态字符串当关键字传给了函数自己的 ``state`` 参数，
    每次都抛 TypeError 并被上层 except 吞掉；账本里只剩 9 条开着的 action，
    finish 于是报了 9 条它自己制造的矛盾。记账失败必须要么写成功、要么留痕。
    """
    state = _fresh(tmp_path)
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    row = ledger.record(state, "action_settled", action_id="a1", state="settled", exit_code=0)
    assert row["kind"] == "action_settled" and row["state"] == "settled"
    job = ledger.record(state, "job_reconciled", nonce="n1", state="submitted", kind_note="ok")
    assert job["state"] == "submitted"
    rows = ledger.open_ledger(state).records()
    assert [r["state"] for r in rows] == ["settled", "submitted"]
    assert ledger.project_actions(rows)[0]["state"] == "settled"
