"""worker 的事件在后端死掉的窗口里仍然是既成事实（RFC 异步运行时 P0-3）。

## 为什么必须跨进程验

要防的事故只在真实的两进程交错里出现：写者是活的（随时半行）、读者会中断
（重连要不丢不重）、而**管道那头会真的消失**。这些在单进程 mock 里全都看
不见 —— [[feedback_tests_dont_bind_project]] 的同款教训。

所以这里起一个真的 worker 进程（真 `JsonlEmitter`、真落盘、stdout 真接管道），
把管道那头真的关掉，再用后端真的续读器去读。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.services.session_event_log import read_events, registry_row

HARNESS_ROOT = Path(__file__).resolve().parents[3]


def _load_platform_runtime():
    spec = importlib.util.spec_from_file_location(
        "platform_runtime_events_under_test", HARNESS_ROOT / "platform_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_runtime = _load_platform_runtime()

#: 一个真 worker：拿真锁（写注册表行）、绑真耐久 sink、往 stdout 发事件。
#: 它每 0.05s 发一条，直到被杀 —— 模拟"正在跑几小时的研究"。
_WORKER = """
import sys, time, pathlib
sys.path.insert(0, sys.argv[1])
import platform_runtime
state_root = pathlib.Path(sys.argv[2])
emit = platform_runtime.JsonlEmitter(sys.stdout, platform_runtime.SecretFilter([]))
with platform_runtime._project_lock(state_root):
    emit.attach_durable_sink(platform_runtime.session_events_path(state_root))
    sys.stderr.write("ready\\n"); sys.stderr.flush()
    for i in range(400):
        emit("progress", request_id="r1", detail="第%d条" % i)
        time.sleep(0.05)
"""


@pytest.fixture
def worker(tmp_path):
    state_root = tmp_path / "orchestrator__proj-1__session__sess-1"
    proc = subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(HARNESS_ROOT), str(state_root), "--serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stderr is not None
    assert "ready" in proc.stderr.readline(), f"worker 没起来 (exit={proc.poll()})"
    try:
        yield proc, state_root
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _wait_for_events(path: Path, count: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file() and len(
            [e for e in read_events(path).events if e.get("type") == "progress"]
        ) >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"{timeout}s 内没等到 {count} 条事件")


def test_the_registry_row_points_at_the_real_events_file(worker):
    """后端靠注册表行找到事件文件 —— 路径规则在两边各写一遍，必须对得上。"""
    _, state_root = worker
    row = registry_row(state_root / ".chat.lock")
    assert row["events_path"] == str(state_root / "events.jsonl")
    assert Path(row["events_path"]).is_file()
    assert row["protocol_version"] == platform_runtime.PROTOCOL_VERSION


def test_events_keep_landing_after_the_pipe_reader_is_gone(worker):
    """**P0 的全部意义**：后端消失了，worker 照跑，事件照落盘。"""
    proc, state_root = worker
    path = state_root / "events.jsonl"
    _wait_for_events(path, 2)

    before = read_events(path)
    assert proc.stdout is not None
    proc.stdout.close()          # 后端死了 —— 管道那头没有读者了
    time.sleep(0.6)              # 让 worker 继续跑几轮

    after = read_events(path, offset=before.offset)
    assert after.events, "管道读者消失后事件停了 —— 这正是要修的病"
    assert proc.poll() is None, "worker 不该因为没人读管道就死掉"

    # 见证：后端重连读到它，就知道中间这段是"它自己不在"，不是 worker 沉默。
    assert any(e["type"] == "protocol_stream_lost" for e in after.events)
    # 研究本身照跑：序号从断点继续，一条不漏一条不重。
    resumed = [e["detail"] for e in after.events if e["type"] == "progress"]
    first_new = len([e for e in before.events if e["type"] == "progress"])
    assert resumed == [f"第{i}条" for i in range(first_new, first_new + len(resumed))]


def test_a_reconnecting_reader_loses_nothing_and_repeats_nothing(worker):
    """重连对账：分多次续读拼起来，必须与从头一次读完逐条相同。"""
    _, state_root = worker
    path = state_root / "events.jsonl"
    _wait_for_events(path, 6)

    stitched, offset = [], 0
    for _ in range(4):                       # 断断续续地读
        batch = read_events(path, offset=offset)
        stitched.extend(e["detail"] for e in batch.events if e["type"] == "progress")
        offset = batch.offset
        time.sleep(0.12)

    whole = [e["detail"] for e in read_events(path).events if e["type"] == "progress"]
    assert stitched == whole[: len(stitched)]
    assert stitched == sorted(set(stitched), key=stitched.index)   # 无重复


def test_the_file_never_hands_out_a_half_written_line(worker):
    """写者是活的：连续快速续读，一条坏行都不该出现。"""
    _, state_root = worker
    path = state_root / "events.jsonl"
    _wait_for_events(path, 2)

    offset, malformed = 0, 0
    for _ in range(40):
        batch = read_events(path, offset=offset)
        malformed += batch.malformed
        offset = batch.offset
        time.sleep(0.02)
    assert malformed == 0


def test_the_events_file_is_valid_jsonl_end_to_end(worker):
    """管道与文件是同一份已脱敏正文 —— 两边分叉时谁都不报错，所以要机械对账。"""
    proc, state_root = worker
    path = state_root / "events.jsonl"
    _wait_for_events(path, 4)
    assert proc.stdout is not None

    piped = [json.loads(proc.stdout.readline()) for _ in range(4)]
    on_disk = [e for e in read_events(path).events if e["type"] == "progress"][:4]
    assert [e["detail"] for e in on_disk] == [e["detail"] for e in piped]
