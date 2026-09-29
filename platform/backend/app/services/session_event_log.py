"""worker 事件文件的**断点续读**（RFC 异步运行时 P0-3）。

## 为什么事件要落文件

事件原来只走 stdout 管道 —— 读者不在就等于没发生。后端重启/崩溃的窗口里
worker 照样在干活、照样在产生进度与转录事件，而这些事件全部蒸发；恢复之后
谁都说不清那段时间发生了什么（「事实不送达」PR#472 的同款形状）。

worker 侧 `platform_runtime.JsonlEmitter` 现在把每条事件先 append 进
`<session_state_root>/events.jsonl`，再往管道写。本模块是读的那一半：从一个
字节偏移接着读，把新事件交出去，并给出新的偏移。

## 两条必须守住的性质

1. **半行不消费**。worker 是活的写者，读者随时可能读到只写了一半的行。
   碰到不以 `\\n` 结尾的尾巴就**留在原地**，偏移停在它前面 —— 下一次读它
   已经完整了。把半行当成"坏记录"跳过，就是在正常写入时随机丢事件。
2. **偏移单调、不猜**。返回的偏移永远指向"已完整消费的最后一个换行之后"。
   调用方拿它当断点，重连不丢不重。

## 偏移存在哪

**不存第二份。** 后端 ingest 已经按 `(file_identity, byte_offset)` 做幂等
（`uq_events_raw_source`），"读到哪了"可以从已入库的事件现算 —— 存一列
就是第二个真相源，而它一定会和事件表分叉，且分叉时谁都不报错
（[[feedback_verdict_vs_evidence]] 的同款：判决不可持久化）。本模块因此
**只接收**起始偏移、**只返回**新偏移，不自己决定它存在哪。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EventBatch:
    """一次续读的结果。"""

    #: 完整读到的事件（坏行已跳过，见 `malformed`）。
    events: list[dict[str, Any]]
    #: 下次从这里接着读。永远指向最后一个完整换行之后。
    offset: int
    #: 跳过的坏行数（JSON 解析失败 / 不是对象）。是**见证**，不是故障：
    #: 一条坏行不该让整个恢复瘫掉，但也不能悄悄咽下去。
    malformed: int = 0
    #: 文件当前是否比给的起始偏移还短 —— 说明它被换过（新 worker 重建了
    #: 会话目录）。调用方据此决定是从头读还是判成新会话，本模块不替它猜。
    truncated: bool = False


def read_events(path: Path, *, offset: int = 0, max_bytes: int = 8 * 1024 * 1024) -> EventBatch:
    """从 `offset` 续读 `path`。文件不存在 → 空批次，偏移原样返回。

    `max_bytes` 是单次读取上限：恢复一个跑了几小时的 session 时，事件文件
    可能很大，一次全读进内存会把后端顶爆。够不完的部分下次接着读 ——
    偏移是断点，多读几次没有副作用。
    """
    path = Path(path)
    if not path.is_file():
        return EventBatch(events=[], offset=offset)

    size = path.stat().st_size
    if size < offset:
        # 文件比断点还短：它被换过（不是"回退了"——append-only 的文件不会缩）。
        return EventBatch(events=[], offset=0, truncated=True)

    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(max(0, max_bytes))

    # 半行不消费：只吃到最后一个换行为止。
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return EventBatch(events=[], offset=offset)
    complete, consumed = chunk[: cut + 1], cut + 1

    events: list[dict[str, Any]] = []
    malformed = 0
    for raw in complete.decode("utf-8", "replace").splitlines():
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        events.append(record)
    return EventBatch(events=events, offset=offset + consumed, malformed=malformed)


def registry_row(lock_path: Path) -> dict[str, Any]:
    """读 worker 的注册表行（`.chat.lock`）。读不到 → 空字典。

    字段（P0-1 起逐步加）：pid / acquired_at / state_root / spawn_token /
    code_version / protocol_version / events_path。**缺字段必须容忍** ——
    注册表是增量演进的，老 worker 写的记录只有前三个，而它可能正跑着一个
    几小时的研究，不能因为记录"不够新"就判它无效。
    """
    try:
        record = json.loads(Path(lock_path).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}
