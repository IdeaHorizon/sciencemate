"""事件日志里的大 payload 只存引用（RFC 异步运行时 P0-7）。

来源是 Codex 的实物事故：一个会话日志被内联 base64 图片撑到 1,476,355,395
字节。我们的事件文件形状完全一样 —— 一条带图的 `transcript` 包装事件同时能
干掉事件文件、协议行长度上限、以及后端恢复时的内存。

与 KB 的「有界读取」同一条不变量：**一次读取的规模不能由被读对象的规模决定**。
"""
from __future__ import annotations

import hashlib
import io
import json

from core.event_blobs import MAX_INLINE_VALUE_BYTES, REF_KEY, blob_dir_for, is_reference
from platform_runtime import JsonlEmitter, SecretFilter

HUGE = "x" * (MAX_INLINE_VALUE_BYTES * 3)


def _emitter(tmp_path, *, durable: bool):
    stream = io.StringIO()
    emitter = JsonlEmitter(stream, SecretFilter([]))
    events = tmp_path / "events.jsonl"
    if durable:
        emitter.attach_durable_sink(events)
    return emitter, stream, events


def test_a_huge_value_becomes_a_receipt_not_a_log_line(tmp_path):
    emitter, stream, events = _emitter(tmp_path, durable=True)
    emitter("transcript", event={"tool_result": {"image_b64": HUGE}})

    line = events.read_text(encoding="utf-8").strip()
    assert len(line) < MAX_INLINE_VALUE_BYTES, "大 payload 还是被写进了日志行"
    ref = json.loads(line)["event"]["tool_result"]["image_b64"]
    assert is_reference(ref)
    assert ref["bytes"] == len(HUGE.encode())
    assert ref["preview"] == HUGE[:200]
    assert stream.getvalue().strip() == line, "文件与管道必须逐字节相同"


def test_the_content_is_still_recoverable(tmp_path):
    """外置不是丢弃：提货单要提得到货，而且是**同一份**货。"""
    emitter, _stream, events = _emitter(tmp_path, durable=True)
    emitter("transcript", event={"payload": HUGE})

    ref = json.loads(events.read_text(encoding="utf-8"))["event"]["payload"]
    blob = (blob_dir_for(events) / f"{ref['sha256']}.bin").read_bytes()
    assert blob == HUGE.encode()
    assert hashlib.sha256(blob).hexdigest() == ref["sha256"]


def test_the_rule_scans_values_not_a_list_of_field_names(tmp_path):
    """判据是"这件事"（值多大），不是"这个字段"（叫什么名字）。

    黑名单式护栏必然漏新字段，而且漏了没人会发现 —— 它只在事故当天才现形。
    这里用一个**从来没有人见过的字段名**验它默认被覆盖。
    """
    emitter, _stream, events = _emitter(tmp_path, durable=True)
    emitter("whatever", a_field_nobody_has_ever_written_before=HUGE)
    record = json.loads(events.read_text(encoding="utf-8"))
    assert is_reference(record["a_field_nobody_has_ever_written_before"])


def test_nested_inside_lists_is_covered_too(tmp_path):
    emitter, _stream, events = _emitter(tmp_path, durable=True)
    emitter("whatever", frames=[{"data": HUGE}, {"data": "small"}])
    frames = json.loads(events.read_text(encoding="utf-8"))["frames"]
    assert is_reference(frames[0]["data"])
    assert frames[1]["data"] == "small"


def test_small_values_are_left_exactly_alone(tmp_path):
    """防线不该改写它不负责的东西 —— 绝大多数事件必须逐字不变。"""
    emitter, _stream, events = _emitter(tmp_path, durable=True)
    emitter("progress", detail="正常大小", count=3, ok=True, items=["a", "b"])
    record = json.loads(events.read_text(encoding="utf-8"))
    assert record["detail"] == "正常大小"
    assert record["count"] == 3 and record["ok"] is True
    assert record["items"] == ["a", "b"]
    assert REF_KEY not in json.dumps(record)


def test_without_a_blob_directory_it_truncates_and_says_so(tmp_path):
    """放不下就**说自己放不下**，绝不静默内联 —— 静默内联就是事故本身。"""
    emitter, stream, _events = _emitter(tmp_path, durable=False)
    emitter("whatever", payload=HUGE)
    record = json.loads(stream.getvalue())
    ref = record["payload"]
    assert is_reference(ref)
    assert ref["dropped"] == "no_blob_directory"
    assert "path" not in ref
    assert len(stream.getvalue()) < MAX_INLINE_VALUE_BYTES


def test_the_same_blob_is_stored_once(tmp_path):
    """内容寻址顺带去重：同一张图发十次，盘上还是一份。"""
    emitter, _stream, events = _emitter(tmp_path, durable=True)
    for _ in range(5):
        emitter("transcript", event={"payload": HUGE})
    assert len(list(blob_dir_for(events).glob("*.bin"))) == 1
