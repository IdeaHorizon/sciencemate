"""会话事件必须落盘 —— 读者不在不等于没发生（RFC 异步运行时 P0-3）。

事件原来只往 App Server 的管道里写。后端重启/崩溃的窗口里 worker 照样在
干活、照样在产生进度与转录事件，而这些事件全部蒸发 —— 恢复之后谁都说不清
那段时间发生了什么（「事实不送达」PR#472 的同款形状）。
"""
from __future__ import annotations

import io
import json

import pytest

import platform_runtime as pr


class _BrokenStream(io.StringIO):
    """写就炸的管道 —— 模拟后端已经死了/连接已断。"""

    def write(self, _s):  # type: ignore[override]
        raise OSError("broken pipe")


def _emitter(stream) -> pr.JsonlEmitter:
    return pr.JsonlEmitter(stream, pr.SecretFilter([]))


def test_events_land_in_the_file_as_well_as_the_pipe(tmp_path):
    path = pr.session_events_path(tmp_path)
    emit = _emitter(io.StringIO())
    emit.attach_durable_sink(path)
    emit("progress", request_id="r1", detail="第一步")
    emit("progress", request_id="r1", detail="第二步")

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["detail"] for r in records] == ["第一步", "第二步"]
    assert all(r["type"] == "progress" and r["at"] for r in records)


def test_the_file_and_the_pipe_agree_byte_for_byte(tmp_path):
    """两边分叉时谁都不报错 —— 所以它们必须是同一份已脱敏正文。"""
    stream = io.StringIO()
    emit = _emitter(stream)
    emit.attach_durable_sink(pr.session_events_path(tmp_path))
    emit("progress", request_id="r1", detail="正文")

    assert pr.session_events_path(tmp_path).read_text(encoding="utf-8") == stream.getvalue()


def test_a_dead_pipe_neither_loses_the_event_nor_kills_the_turn(tmp_path):
    """**这条是 P0 的全部意义**：后端死了，事件仍是既成事实，而且这一轮不死。

    实测（真两进程）：读端一消失，下一次 stdout 写就 BrokenPipeError，worker
    当场 exit 120 —— 它可能正跑到一个几小时轮次的中途。一个**转发面**没有了，
    代价却是研究本身被销毁。
    """
    path = pr.session_events_path(tmp_path)
    emit = _emitter(_BrokenStream())
    emit.attach_durable_sink(path)

    emit("progress", request_id="r1", detail="后端已经死了")   # 不抛
    emit("progress", request_id="r1", detail="我接着跑")       # 断过之后也不抛

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["detail"] for r in records if r["type"] == "progress"] == [
        "后端已经死了", "我接着跑",
    ]
    # 见证进事实面：后端重连读到它，就知道中间这段是"它自己不在"。
    assert [r["type"] for r in records].count("protocol_stream_lost") == 1


def test_without_a_sink_a_dead_pipe_still_raises(tmp_path):
    """边界：没绑 sink 时管道**就是**唯一送达面 —— 咽下去等于让调用方
    永远等一个不会来的结果。一次性执行 / CLI 走的正是这条路。"""
    emit = _emitter(_BrokenStream())
    with pytest.raises(OSError):
        emit("progress", request_id="r1", detail="没有别的送达面")


def test_a_dead_sink_does_not_take_down_the_research(tmp_path):
    """反向：盘写不进去也不该带走正在跑的研究，管道那份照发。"""
    stream = io.StringIO()
    emit = _emitter(stream)
    emit.attach_durable_sink(pr.session_events_path(tmp_path))
    emit._sink.close()  # 盘那头没了

    emit("progress", request_id="r1", detail="盘挂了")
    assert "盘挂了" in stream.getvalue()


def test_unbound_emitter_behaves_exactly_as_before(tmp_path):
    """一次性执行路径 / CLI / init 之前的事件走老路，不该因为这层改变行为。"""
    stream = io.StringIO()
    emit = _emitter(stream)
    emit("progress", request_id="r1", detail="没有 sink")
    assert json.loads(stream.getvalue())["detail"] == "没有 sink"
    assert not (tmp_path / "events.jsonl").exists()


def test_appends_across_restarts_so_offsets_stay_monotonic(tmp_path):
    """append-only：后端按字节偏移断点续读，重连不丢不重。"""
    path = pr.session_events_path(tmp_path)
    first = _emitter(io.StringIO())
    first.attach_durable_sink(path)
    first("progress", request_id="r1", detail="重启前")
    offset = path.stat().st_size

    second = _emitter(io.StringIO())          # 新进程接着写同一个文件
    second.attach_durable_sink(path)
    second("progress", request_id="r2", detail="重启后")

    with path.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        tail = [json.loads(line) for line in fh.read().splitlines()]
    assert [r["detail"] for r in tail] == ["重启后"]


def test_an_unusable_sink_path_does_not_block_startup(tmp_path):
    """绑定失败 → 不绑定、照常跑：耐久是增强，不是能不能开工的前提。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    stream = io.StringIO()
    emit = _emitter(stream)
    emit.attach_durable_sink(blocker / "nested" / "events.jsonl")

    emit("progress", request_id="r1", detail="照常跑")
    assert "照常跑" in stream.getvalue()
