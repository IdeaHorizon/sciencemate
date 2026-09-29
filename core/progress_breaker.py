"""进展熔断 —— 按"这一轮有没有发生什么"停机，不按工具名单、不按轮数。

## 为什么需要它

2026-08-11 拍板：防空转不再数轮次（轮数区分不了"跑得久"和"原地转"），交给
按**进展**判的那几层。这个模块就是那句话缺的机械定义。

现有那几道都盯着**具体的东西**，于是各自有各自的盲区：

  - `ProtocolFailureBreaker` 盯 tool-call 协议崩坏 —— 协议正常的死循环看不见
  - `tool_call_cache` 盯"同一个工具 + 同样参数"，且靠一张**硬编码白名单**
    （`CACHEABLE_TOOLS`）判断哪些工具算数
  - 空轮回滚盯"无工具调用 + 无正文"

v26 的 1482 轮空转从这三道中间穿过去了：协议全程正常；每轮 `write_scratchpad`
的参数都不一样（缓存对不上）；而 `read_file` 压根不在那张白名单里 —— 它
**早就在自己的 ToolDefinition 上声明了 `replayable_read=True`**，只是名单没去看。

**信号取"有没有变化"，不取"调的是哪个工具"，白名单这类缺陷在结构上就不存在。**

## 两个计数器，两种性质

`repeat`（判决）—— 模型这一轮的输出与上一轮**逐字节相同**，且这一轮没有留下
任何持久变化。温度不为零的采样几乎不可能连续复读；连续复读 N 轮 = 输入实际上
没变，模型被困住了。误报率极低，所以它可以停机。

`stall`（证据）—— 连续多轮没有任何持久变化，但输出在变。这可能是正常的：读
文献、翻目录、连着想事情，都可以几十轮不落盘。所以它**只提示不停机** ——
把事实送到模型面前（"你已经 N 轮没有产生任何持久变化"），判断留给模型。

判决与证据分开，是因为它们的代价不对称：误停一条正在正常工作的 run，比多说
一句话贵得多。
"""

from __future__ import annotations

import hashlib
import json
import os
import zlib
from dataclasses import dataclass, field
from typing import Any

#: 连续复读多少轮 → 警告 / 停机。
_REPEAT_WARN = 3
_REPEAT_ABORT = 6
#: 连续多少轮没有持久变化 → 提示一次（只提示，不停机；之后每这么多轮再提一次）。
_STALL_NOTICE_EVERY = 15


def _env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return fallback


#: 退化复读的判据：**尾部**这么多字符里的压缩比。
#:
#: 为什么看尾部而不是整段（2026-08-17 实测定的）：真实样本里有一条 34,339 字
#: 的输出，开头是货真价实的论文清单（Hussain 2025、Meier 2004、真 DOI），
#: 结尾崩成 `.__.__.__.__…` 一路撞到 token 上限。整段压缩比被好开头稀释到
#: 0.0412 混在正常样本里；只看尾部立刻掉到 0.0147。**退化发生在尾部。**
_TAIL_WINDOW = 1500

#: 两档，对应本模块一贯的「判决 vs 证据」分法：
#:   NO_BUDGET_RAISE —— 宽，只用来**不再加预算**。对着复读加预算永远是错的，
#:                      误判的代价只是这一轮不加预算，很小。
#:   DEGENERATE      —— 严，用来**停机**。误停一条正常 run 很贵，所以取在
#:                      实测间隙的下沿。
#:
#: 实测分布（67 条 ≥2000 字的输出，按尾部 1500 字压缩比）：
#:   ≤0.0418  11 条，全部肉眼可见是垃圾（帚帚帚帚 / 罚/罚/ / ://:// / 677_677_）
#:   0.0640   制表符刷屏
#:   0.0850   门禁报错文本被复述（内容本身是真的）
#:   ≥0.1731  全部是正常收尾的中英文散文与代码
_DEGENERATE_TAIL_RATIO = 0.05
_NO_BUDGET_RAISE_TAIL_RATIO = 0.10


def tail_repetition_ratio(text: str | None, window: int = _TAIL_WINDOW) -> float:
    """输出尾部的压缩比。越小 = 越重复。空文本返回 1.0（当作"不重复"）。

    用 zlib 而不是自己数 n-gram：它对"同一个词组反复"和"同一个字符反复"
    一视同仁，而实测两种都出现过（`我已经识出核心子。`×N 和 `帚帚帚帚`×N）。
    """
    if not text:
        return 1.0
    blob = text[-window:].encode("utf-8", "replace")
    if not blob:
        return 1.0
    return len(zlib.compress(blob, 6)) / len(blob)


def is_degenerate_repetition(text: str | None) -> bool:
    """这段输出是不是已经退化成复读 —— 用于**停机**判决，取严的那一档。"""
    return tail_repetition_ratio(text) <= _DEGENERATE_TAIL_RATIO


def should_not_raise_budget(text: str | None) -> bool:
    """要不要**停止给它加输出预算** —— 取宽的那一档。

    实测事故：截断恢复看到 `finish=length` 就把预算从 16,384 抬到 32,768，
    而模型正在复读 —— 框架自己把这一轮的浪费翻了一倍。加预算只对"内容真的
    写不下"有用，对"复读写不完"是火上浇油，而这两者靠尾部压缩比分得开。
    """
    return tail_repetition_ratio(text) <= _NO_BUDGET_RAISE_TAIL_RATIO


def response_signature(content: str | None, tool_calls: Any) -> str:
    """这一轮模型说了什么 + 要调什么 —— 逐字节。

    工具调用连**参数**一起进签名：只比工具名会把"读了不同的文件"误判成复读。
    """
    payload: list[Any] = [(content or "").strip()]
    for call in (tool_calls or []):
        if isinstance(call, dict):
            fn = call.get("function") or {}
            payload.append([fn.get("name") or call.get("name"),
                            fn.get("arguments") or call.get("arguments")])
        else:
            payload.append([getattr(call, "name", None), getattr(call, "arguments", None)])
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def durable_signature(state: Any) -> str:
    """这一轮之后，**持久世界**长什么样。变了 = 有进展。

    三个来源，都是现成的、每轮已经在维护的东西 —— 不为熔断新增扫盘开销：

      - Project 工作区指纹：`observe_after_tool` 每次工具调用后已算好。它覆盖
        产物、图、脚本、checkpoint —— 凡是落进节点 Git 目录的都算。
      - 白板改写次数：模型主动改写工作状态也是进展。
      - run-local 产物数：没绑 worktree 时（CLI / 单机跑）唯一的落盘信号。
    """
    parts: list[str] = []
    hook_state = getattr(state, "hook_state", None) or {}
    from core.project_workspace import FINGERPRINT_KEY

    parts.append(str(hook_state.get(FINGERPRINT_KEY) or ""))

    from core.whiteboard import signature as _board_signature

    parts.append(str(_board_signature(state)))

    if not getattr(state, "project_worktree", None):
        try:
            parts.append(str(len(state.list_artifacts())))
        except Exception:
            parts.append("?")
    return "|".join(parts)


@dataclass
class ProgressDecision:
    repeat_streak: int = 0
    stall_streak: int = 0
    should_warn: bool = False
    should_abort: bool = False
    diagnosis: str = ""


@dataclass
class ProgressBreaker:
    """每轮末恰好调一次 `record`。

        d = progress.record(state, response_signature(content, tool_calls), turn=turn)
        if d.should_abort:
            return LoopResult(final_text=d.diagnosis, status="failed", ...)
        if d.should_warn:
            messages.append(framework_notice(d.diagnosis))   # ← 不是 role="system"
    """

    repeat_warn_at: int = field(
        default_factory=lambda: _env_int("HARNESS_REPEAT_WARN", _REPEAT_WARN))
    repeat_abort_at: int = field(
        default_factory=lambda: _env_int("HARNESS_REPEAT_ABORT", _REPEAT_ABORT))
    stall_notice_every: int = field(
        default_factory=lambda: _env_int("HARNESS_STALL_NOTICE_EVERY", _STALL_NOTICE_EVERY))

    _last_response: str | None = None
    _last_durable: str | None = None
    repeat_streak: int = 0
    stall_streak: int = 0

    def record(self, state: Any, response: str, *, turn: int = 0) -> ProgressDecision:
        durable = durable_signature(state)
        moved = self._last_durable is not None and durable != self._last_durable
        first = self._last_durable is None
        self._last_durable = durable

        if moved or first:
            self.stall_streak = 0
        else:
            self.stall_streak += 1

        # 复读只在**没有留下任何持久变化**时才算。一个模型可以连说两轮同样的话，
        # 但只要它落了盘，那两轮就不是原地转。
        if response == self._last_response and not moved:
            self.repeat_streak += 1
        else:
            self.repeat_streak = 1 if response == self._last_response else 0
        self._last_response = response

        should_abort = (self.repeat_abort_at > 0
                        and self.repeat_streak >= self.repeat_abort_at)
        should_warn = (not should_abort
                       and self.repeat_warn_at > 0
                       and self.repeat_streak >= self.repeat_warn_at)
        stall_notice = (not should_abort and not should_warn
                        and self.stall_notice_every > 0
                        and self.stall_streak > 0
                        and self.stall_streak % self.stall_notice_every == 0)

        return ProgressDecision(
            repeat_streak=self.repeat_streak,
            stall_streak=self.stall_streak,
            should_warn=should_warn or stall_notice,
            should_abort=should_abort,
            diagnosis=self._diagnosis(should_abort, should_warn, stall_notice, turn),
        )

    def _diagnosis(self, abort: bool, warn: bool, stall: bool, turn: int) -> str:
        if abort:
            return (
                f"⛔ 进展熔断：连续 {self.repeat_streak} 轮，你的回复与上一轮**逐字节相同**，"
                f"且这期间没有任何持久变化（没有产物、没有文件改动、没有改写白板）。\n"
                f"同样的输入只会给出同样的输出 —— 再跑下去不会变。停机。\n"
                f"典型成因：你想做的那件事没有对应的工具，而框架没有告诉你做不到；"
                f"或者你需要的上下文每轮都被压缩掉了。"
            )
        if warn:
            return (
                f"⚠️ 你已经连续 {self.repeat_streak} 轮给出**一字不差**的回复，"
                f"且没有留下任何持久变化。再重复 "
                f"{max(0, self.repeat_abort_at - self.repeat_streak)} 轮本 run 会被停掉。\n"
                f"换一个动作：把当前状态整块写进白板（write_scratchpad）、"
                f"改用别的工具、或者用 request_human_input 说清楚你卡在哪。\n"
                f"如果你要做的事**没有对应的工具**，直接说出来并停下，别再试同一条路。"
            )
        if stall:
            return (
                f"📊 事实：你已经 {self.stall_streak} 轮没有产生任何持久变化"
                f"（无产物、无文件改动、无白板改写），当前 turn {turn}。\n"
                f"如果这是有意的（还在读、还在想），忽略这条。"
                f"如果不是，现在是把进展落盘的时候。"
            )
        return ""
