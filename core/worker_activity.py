"""worker 的**活动**：此刻有没有计算在飞（RFC 异步运行时 D10 活动维 + P0-6）。

## 三个概念各归各位

2026-08-21 那次事故的病根是一把 operation lock 被迫回答三个不同的问题，而它
一个都答不对。三个概念从此各有各的载体：

- **所有权**（谁可以写这个工作区）= `.chat.lock` 的 flock。长寿命、跨睡眠持有、
  内容**写一次就不再改**。它回答"谁拥有"，永远不用来回答"在不在干活"。
- **活动**（此刻有没有计算在飞）= 本模块的 `activity.json`。worker 自报，
  带心跳租约；沉默衰减为 `unknown`。
- **可寻址**（这句话能不能送达）= 恒真，不需要任何文件来证明。

## 为什么活动不能塞进锁文件

flock 挂在**那个 inode** 上。要原子地换掉内容只有 `os.replace`，而它换的正是
inode —— 锁就此失效，且两个进程都不会报错（一个还以为自己攥着，另一个抢得到）。
所以活动必须是另一个文件：它高频改写（每次状态变化 + 心跳），锁文件一辈子只
写一次。

## 心跳由干活的那个循环自己发

不起心跳线程。`touch()` 挂在 worker 的**事件发射口**上 —— 有事件产生才有心跳，
心跳因此与真实进展耦合。独立线程只能证明"这个进程还在被调度"，那恰好是
「看起来活着的僵尸」的制造方法。

## 读出来的 `unknown` 只呈现，不判决

租约过期的意思是"我们不知道"，不是"它死了"。本模块因此只**计算**状态，不提供
任何"所以可以杀掉它"的判据 —— 杀不杀由人、或者由别处一条显式的机械政策定。

## 谁写、谁读

写：harness worker（`platform_runtime --serve`），进程内唯一写者。
读：App Server（重启后接回 worker 时重建绑定、以及回答"它在干什么"）。
两边共享本模块，不各写一份 —— 格式抄一份就会分叉，而分叉时两边都不报错。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── 活动维的取值 ─────────────────────────────────────────────────────────────
#: 有计算在飞。心跳来自真实事件流。
WORKING = "working"
#: 主动停靠到某个时刻（unattended 的复查间隔）。声明了 `until` 与 `why`。
PARKED = "parked"
#: 停在一个问题上等人。可以合法地静默任意久。
WAITING_HUMAN = "waiting_human"
#: 活着，没活干。
IDLE = "idle"
#: **读出来的**状态，worker 永远不会写它：租约到期而没有新消息。
UNKNOWN = "unknown"

#: worker 自报的合法取值（`UNKNOWN` 不在内 —— 它是观察结果，不是自报）。
DECLARABLE_STATES = (WORKING, PARKED, WAITING_HUMAN, IDLE)

#: 心跳最多这么频繁地落一次盘。事件流比这密得多，节流是为了不让一次 turn
#: 产生几万次写。
HEARTBEAT_INTERVAL_SECONDS = 15.0
#: `working` 的租约。留足心跳间隔的数倍余量 —— 一次慢 IO、一次长模型调用
#: 不该把一个正常干活的 worker 读成 unknown。
#:
#: ⚠️ 这个值必须大于**单次 LLM 调用**可能的最长静默（实测 GPUStack 上
#: 8 万 token prompt 单次 115s）。取 10 分钟：宁可晚一点说"不知道"，也不要
#: 频繁地对一个健康的 worker 说不知道。
WORKING_LEASE_SECONDS = 600.0
#: `parked` 的租约是它自己声明的 `until`，再加这一段宽限 —— 醒来之后要走一段
#: 路才会产生下一条事件。
PARK_GRACE_SECONDS = 300.0

#: 活动文件名。与 `.chat.lock` 同目录（同生共死，不发明第二个运行时目录约定）。
ACTIVITY_FILENAME = "activity.json"
#: 所有权锁的文件名。规则写在这里，两个进程都从这里取。
LOCK_FILENAME = ".chat.lock"


def session_dir_name(project_id: str, session_id: str) -> str:
    """一个 session 的运行时目录名。

    这条规则从前在 App Server 与 worker 各写了一遍，注释里写着"是没办法的事"。
    有了契约桥之后它就有办法了 —— 抄件会各自演化，而分叉时两边都不报错。
    """
    return f"orchestrator__{project_id}__session__{session_id}"


def lock_path(state_root: Path | str) -> Path:
    """所有权锁的位置。"""
    return Path(state_root) / LOCK_FILENAME


def activity_path(state_root: Path | str) -> Path:
    """活动文件的位置。"""
    return Path(state_root) / ACTIVITY_FILENAME


@dataclass(frozen=True)
class Activity:
    """一次观察的结果。

    `declared` 是 worker 自己说的，`state` 是把租约算进去之后**观察到**的
    —— 两者分开保留，因为"它说它在干活，但已经十分钟没动静了"和"它说它空闲"
    是完全不同的两件事，合并成一个字段就再也分不出来。
    """

    #: 文件在不在。不在 = 这个 session 没有 worker 自报过活动（不等于没有 worker：
    #: 老版本的 worker 不写这个文件）。
    present: bool = False
    #: worker 自报的取值。
    declared: str = IDLE
    #: 把租约算进去之后的取值，可能是 `UNKNOWN`。
    state: str = IDLE
    #: 进入当前状态的时刻（epoch 秒）。
    since: float = 0.0
    #: 最后一次心跳（epoch 秒）。
    heartbeat_at: float = 0.0
    #: 状态自带的事实：working→{"step"}、parked→{"until","why"}、
    #: waiting_human→{"question","pause_id"}。
    detail: dict[str, Any] = field(default_factory=dict)
    #: 当前在飞的那次 RPC 的 request_id。空 = 没有在飞的请求。
    turn_id: str = ""
    #: 派这一轮的调用方自报的绑定（App Server 的 run/session/user）。worker
    #: 不解释它，只如实带着 —— 它是"这进程是哪一轮为了什么起的"的答案。
    app_binding: dict[str, Any] = field(default_factory=dict)
    #: 进程的 argv 与启动时刻（事后取证用；抄自 Codex process_manager）。
    command: tuple[str, ...] = ()
    started_at: float = 0.0
    pid: int = 0
    spawn_token: str = ""

    @property
    def occupied(self) -> bool:
        """有没有计算在飞。

        `UNKNOWN` **算占用** —— "我们不知道它在不在跑"的正确行为是别去开第二轮，
        不是假设它闲着。这不是判决它死活（那件事本模块不回答），只是不去撞它。
        """
        return self.state in (WORKING, UNKNOWN)


def _decay(declared: str, *, heartbeat_at: float, detail: dict, now: float) -> str:
    """把自报状态与租约合成观察状态。

    只有 `working` 与 `parked` 有租约：
      · `working` 的租约是心跳 —— 干活就该有事件产出，静默太久就是不知道了；
      · `parked` 的租约是它自己声明的 `until`（+ 宽限）—— 停靠期间**本来就
        没有事件**，拿心跳去量它只会把每一次正常停靠都读成 unknown。
    `waiting_human` 与 `idle` 没有租约：它们可以合法地静默到天荒地老，
    死活由"进程还在不在"回答，不由本模块回答。
    """
    if declared == WORKING:
        return WORKING if now - heartbeat_at <= WORKING_LEASE_SECONDS else UNKNOWN
    if declared == PARKED:
        until = float(detail.get("until") or 0.0)
        return PARKED if now <= until + PARK_GRACE_SECONDS else UNKNOWN
    return declared


def read_activity(path: Path | str, *, now: float | None = None) -> Activity:
    """读一次活动。读不到 / 读到坏文件 → `present=False` 的空活动。

    读不到**不是**故障：老 worker 不写这个文件，而它可能正跑着一个几小时的
    研究。调用方据此回退到别的判据，不要把"没有自报"当成"不在干活"。
    """
    now = time.time() if now is None else now
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return Activity()
    if not isinstance(raw, dict):
        return Activity()
    declared = str(raw.get("state") or IDLE)
    if declared not in DECLARABLE_STATES:
        # 不认识的取值不猜、也不当成空 —— 如实降级成"不知道"。
        declared = IDLE if not raw.get("state") else UNKNOWN
    detail = raw.get("detail")
    detail = detail if isinstance(detail, dict) else {}
    binding = raw.get("app_binding")
    binding = binding if isinstance(binding, dict) else {}
    command = raw.get("command")
    command = tuple(str(part) for part in command) if isinstance(command, list) else ()
    heartbeat_at = float(raw.get("heartbeat_at") or 0.0)
    state = (
        UNKNOWN
        if declared == UNKNOWN
        else _decay(declared, heartbeat_at=heartbeat_at, detail=detail, now=now)
    )
    return Activity(
        present=True,
        declared=declared,
        state=state,
        since=float(raw.get("since") or 0.0),
        heartbeat_at=heartbeat_at,
        detail=detail,
        turn_id=str(raw.get("turn_id") or ""),
        app_binding=binding,
        command=command,
        started_at=float(raw.get("started_at") or 0.0),
        pid=int(raw.get("pid") or 0),
        spawn_token=str(raw.get("spawn_token") or ""),
    )


class ActivityWriter:
    """worker 侧的写入面 —— 一个进程一个实例，它是唯一写者。

    落盘一律 `os.replace` 原子替换：读者是另一个进程，它没有锁，读到半个
    JSON 的后果是"这个 worker 看起来没自报过活动"，而那会让它被当成接不回来。
    """

    def __init__(
        self,
        path: Path | str,
        *,
        pid: int | None = None,
        spawn_token: str = "",
        command: list[str] | tuple[str, ...] | None = None,
        started_at: float | None = None,
    ) -> None:
        self._path = Path(path)
        self._pid = int(pid if pid is not None else os.getpid())
        self._spawn_token = spawn_token
        self._command = tuple(str(part) for part in (command or ()))
        self._started_at = float(started_at if started_at is not None else time.time())
        self._lock = threading.Lock()
        self._state = IDLE
        self._since = self._started_at
        self._detail: dict[str, Any] = {}
        self._turn_id = ""
        self._binding: dict[str, Any] = {}
        self._last_write = 0.0

    # ── 状态迁移 ────────────────────────────────────────────────────────────
    def set_state(
        self,
        state: str,
        *,
        detail: dict[str, Any] | None = None,
        turn_id: str | None = None,
        app_binding: dict[str, Any] | None = None,
    ) -> None:
        """自报一个新状态，立刻落盘（状态变化不节流）。

        回到 `idle` 会把 `turn_id`、`app_binding`、`detail` 一并清空 ——
        「从不更新的字段不是事实」：留着上一轮的 request_id 会让事后取证读出
        一个早就结束的轮次，比没有更糟。
        """
        if state not in DECLARABLE_STATES:
            raise ValueError(
                f"worker 只能自报 {DECLARABLE_STATES} 之一，收到 {state!r}"
                f"（{UNKNOWN} 是观察结果，不是自报值）"
            )
        with self._lock:
            self._state = state
            self._since = time.time()
            if state == IDLE:
                self._detail = {}
                self._turn_id = ""
                self._binding = {}
            else:
                self._detail = dict(detail or {})
                if turn_id is not None:
                    self._turn_id = turn_id
                if app_binding is not None:
                    self._binding = dict(app_binding)
            self._write_locked(time.time())

    def touch(self, *, force: bool = False) -> None:
        """心跳。由**产生事件的那条路径**调用，按 `HEARTBEAT_INTERVAL_SECONDS` 节流。"""
        now = time.time()
        with self._lock:
            if not force and now - self._last_write < HEARTBEAT_INTERVAL_SECONDS:
                return
            self._write_locked(now)

    def close(self) -> None:
        """进程正常退场：删掉文件。

        "没有活动文件"与"idle"是两件不同的事，别用后者冒充前者 —— 进程都没了，
        它的自报活动就该消失，而不是永远停在"它说它空闲"。
        """
        with self._lock:
            try:
                self._path.unlink()
            except OSError:
                pass

    # ── 落盘 ────────────────────────────────────────────────────────────────
    def _write_locked(self, now: float) -> None:
        record = {
            "state": self._state,
            "since": self._since,
            "heartbeat_at": now,
            "detail": self._detail,
            "turn_id": self._turn_id,
            "app_binding": self._binding,
            "command": list(self._command),
            "started_at": self._started_at,
            "pid": self._pid,
            "spawn_token": self._spawn_token,
        }
        blob = json.dumps(record, ensure_ascii=False)
        tmp = self._path.with_name(f"{self._path.name}.{self._pid}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(blob, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            # 自报失败不该杀掉一次真研究。代价是这段时间读者看到的是旧记录
            # （最坏情况：租约过期读成 unknown，而 unknown 只呈现不判决）。
            with_suppress = getattr(tmp, "unlink", None)
            if with_suppress is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return
        self._last_write = now
