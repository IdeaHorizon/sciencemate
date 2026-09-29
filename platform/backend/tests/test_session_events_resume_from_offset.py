"""事件文件的断点续读（RFC 异步运行时 P0-3）。

判据全部对着**真文件**，因为要防的两个事故都只在真实写读交错时出现：
读到半行、以及重连后丢/重事件。
"""
from __future__ import annotations

import json

from app.services.session_event_log import read_events, registry_row


def _write(path, *records, partial: str = "") -> None:
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if partial:
            fh.write(partial)  # 故意不带换行 —— 模拟 worker 正写到一半


def test_reads_everything_from_the_start(tmp_path):
    path = tmp_path / "events.jsonl"
    _write(path, {"type": "progress", "detail": "一"}, {"type": "progress", "detail": "二"})

    batch = read_events(path)
    assert [e["detail"] for e in batch.events] == ["一", "二"]
    assert batch.offset == path.stat().st_size


def test_resumes_without_loss_or_duplication(tmp_path):
    """重连的判据：接着读只拿到新的，且一条不漏。"""
    path = tmp_path / "events.jsonl"
    _write(path, {"type": "progress", "detail": "断线前"})
    first = read_events(path)

    _write(path, {"type": "progress", "detail": "断线中"}, {"type": "progress", "detail": "重连后"})
    second = read_events(path, offset=first.offset)

    assert [e["detail"] for e in second.events] == ["断线中", "重连后"]
    assert second.offset == path.stat().st_size


def test_a_half_written_line_is_left_for_next_time(tmp_path):
    """**最关键的一条**：worker 是活的写者，读者随时会读到半行。
    把半行当坏记录跳过 = 正常写入时随机丢事件。"""
    path = tmp_path / "events.jsonl"
    _write(path, {"type": "progress", "detail": "完整"}, partial='{"type": "progr')

    batch = read_events(path)
    assert [e["detail"] for e in batch.events] == ["完整"]
    assert batch.malformed == 0                      # 半行不算坏行
    assert batch.offset < path.stat().st_size        # 偏移停在半行之前

    # worker 把那行写完 → 下次读到完整的它，不重不漏。
    with path.open("a", encoding="utf-8") as fh:
        fh.write('ess", "detail": "补完了"}\n')
    assert [e["detail"] for e in read_events(path, offset=batch.offset).events] == ["补完了"]


def test_nothing_new_yields_an_empty_batch_at_the_same_offset(tmp_path):
    path = tmp_path / "events.jsonl"
    _write(path, {"type": "progress", "detail": "唯一一条"})
    first = read_events(path)
    again = read_events(path, offset=first.offset)
    assert again.events == [] and again.offset == first.offset


def test_a_bad_line_is_witnessed_not_fatal(tmp_path):
    """一条坏行不该让整个恢复瘫掉 —— 但也不能悄悄咽下去。"""
    path = tmp_path / "events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write('{"type": "progress", "detail": "好的"}\n')
        fh.write("这不是 JSON\n")
        fh.write('["不是对象"]\n')
        fh.write('{"type": "progress", "detail": "后面的照读"}\n')

    batch = read_events(path)
    assert [e["detail"] for e in batch.events] == ["好的", "后面的照读"]
    assert batch.malformed == 2


def test_a_replaced_file_is_reported_not_guessed(tmp_path):
    """文件比断点还短 = 它被换过（append-only 的文件不会缩）。
    从头读还是判成新会话，是调用方的决定，本模块不替它猜。"""
    path = tmp_path / "events.jsonl"
    _write(path, {"type": "progress", "detail": "旧会话很长很长很长"})
    stale_offset = path.stat().st_size

    path.write_text('{"type": "progress", "detail": "新的"}\n', encoding="utf-8")
    batch = read_events(path, offset=stale_offset)
    assert batch.truncated is True and batch.offset == 0


def test_a_missing_file_is_not_an_error(tmp_path):
    """worker 还没绑定 sink（init 之前）就没有这个文件 —— 不是故障。"""
    batch = read_events(tmp_path / "nope.jsonl", offset=17)
    assert batch.events == [] and batch.offset == 17


def test_a_large_backlog_is_read_in_bounded_chunks(tmp_path):
    """恢复一个跑了几小时的 session：一次全读进内存会把后端顶爆。
    分批读完，且跨批不丢不重。"""
    path = tmp_path / "events.jsonl"
    _write(path, *({"type": "progress", "detail": f"第{i}条"} for i in range(500)))

    seen, offset = [], 0
    while True:
        batch = read_events(path, offset=offset, max_bytes=256)
        if not batch.events:
            break
        seen.extend(e["detail"] for e in batch.events)
        offset = batch.offset
    assert seen == [f"第{i}条" for i in range(500)]
    assert offset == path.stat().st_size


# ── 注册表行 ────────────────────────────────────────────────────────────────

def test_registry_row_reads_the_worker_record(tmp_path):
    lock = tmp_path / ".chat.lock"
    lock.write_text(json.dumps({
        "pid": 4242, "spawn_token": "tok", "code_version": "abc123",
        "protocol_version": 1, "events_path": "/x/events.jsonl",
    }), encoding="utf-8")
    row = registry_row(lock)
    assert row["spawn_token"] == "tok" and row["events_path"] == "/x/events.jsonl"


def test_an_old_record_missing_new_fields_is_still_valid(tmp_path):
    """老 worker 写的记录只有 pid/acquired_at/state_root —— 而它可能正跑着
    一个几小时的研究，不能因为记录"不够新"就判它无效。"""
    lock = tmp_path / ".chat.lock"
    lock.write_text(json.dumps({"pid": 4242, "acquired_at": "2026-08-18T00:00:00+00:00"}),
                    encoding="utf-8")
    row = registry_row(lock)
    assert row["pid"] == 4242
    assert row.get("spawn_token", "") == ""


def test_an_unreadable_record_is_an_empty_dict(tmp_path):
    lock = tmp_path / ".chat.lock"
    lock.write_text("{ broken", encoding="utf-8")
    assert registry_row(lock) == {}
    assert registry_row(tmp_path / "missing.lock") == {}
