"""白板 —— run 内的跨轮工作状态。**覆盖语义:一块板子，不是一本日志。**

## 为什么重写（2026-08-13 实测代价）

旧设计的提示语许诺的是一块状态板：

    "写：如果下一轮的我没看到现在的 messages、只看到压缩摘要 + scratchpad，
      他/她需要知道什么才能高效继续推进？"

那是**覆盖语义**的心智模型 —— 一块随时反映现状的板子。但 API 给的是
`append`：只能往后加，永远擦不掉。文案和机制说了两样话，模型照着文案理解、
被机制惩罚。

实测后果（v26 的 hypothesis，一条 run）：

    turn  891 → 54 条笔记
    turn  948 → 107 条          每一轮正好 +1 条，从 891 一直加到 1482
    turn 1479 → 637 条  ≈ 50KB

    每轮把这 50KB 原样注入 → 窗口被它吃掉大半 → 触发压缩 →
    压缩只能压掉**真实工作**（工具结果、推理），压不掉 hook 注入
    （下一轮又原样来一份）→ 模型每轮看到的东西几乎完全一样 →
    **输出一字不变** → 而它唯一能"记下我意识到出问题了"的动作就是再写一条

    1482 轮 / 117M tokens / 5h40m，占该项目 token 的 79%。

    最后几百轮模型每轮的原话：
      "I need to break the scratchpad loop. Let me immediately write the
       replacement content file and execute the Python script."

    它诊断对了。它唯一的工具就是那个正在害它的东西 —— 想擦板子，手里只有笔，
    没有板擦。

## 新语义

- **写 = 整块覆盖。** 世界上不存在"第 640 条笔记"了 —— 由构造保证不增长。
- **容量硬上限，写入口机械拒绝。** 大小是机械可判的，框架管；留什么擦什么是
  语义判断，模型管。旧设计恰好反了：框架不管大小（该机械管的没管），却指望
  模型自己节制（把机械问题丢给了模型）。
- **不做"追加 + 框架淘汰最旧几条"。** 淘汰哪条是语义判断，框架不该替模型做。

同一语义在项目层已经存在：`MEMORY.md` 是策展修订、不是追加
（"compact superseded entries instead of creating a second memory authority"）。
白板就是它的 run 级版本。
"""

from __future__ import annotations

import os
from typing import Any

#: 白板容量（tokens）。板子是"给下一轮的自己的便条"，不是档案馆。
_DEFAULT_CAPACITY_TOKENS = 1000
_CAPACITY_ENV = "HARNESS_WHITEBOARD_MAX_TOKENS"


def capacity_tokens() -> int:
    """白板容量。部署可用 env 调，但**永远有限** —— 0/负数/垃圾值都落回默认。"""
    raw = os.environ.get(_CAPACITY_ENV)
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return _DEFAULT_CAPACITY_TOKENS


def measure(text: str) -> int:
    """白板正文的 token 估算 —— 与压缩器同一个量具，不另起一套。"""
    from core.summarizer import estimate_text_tokens

    return estimate_text_tokens(text or "")


def adopt_legacy(value: Any) -> str:
    """把旧格式（list[str] 追加日志）读成白板正文。

    续连一条旧 checkpoint 时必经此处。旧笔记不丢 —— 但从此刻起它是**一块板子**，
    模型下一次写入就会整体改写它。超出容量不在这里截断：截断哪一段是语义判断，
    留给模型第一次改写时决定；注入侧只负责把超容量这件事说清楚（见 `render`）。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value)


def write(state: Any, content: str, *, turn: int = 0) -> dict:
    """整块改写白板。超容量 **拒绝写入且不改动原板** —— 让模型自己取舍。

    空 content = **擦板**：物理可行、账不假（transcript 照记一次改写）、无资源
    风险；`render` 对空板返回 None，下一轮就是没有白板。「别交白卷」曾是这里
    的一道拒绝，那是偏好不是判据（判决拆除 2026-09-02）。
    """
    text = (content or "").strip()
    limit = capacity_tokens()
    size = measure(text)
    if size > limit:
        return {
            "status": "error",
            "error": (
                f"白板放不下：{size} tokens，容量 {limit}。板子是给下一轮的自己的便条，"
                f"不是档案馆 —— 删掉已经做完的、已经排除的、写进 artifact 的，"
                f"只留「我在哪 / 下一步 / 哪些路已经堵死」。\n"
                f"结论和发现请写 artifact；可复用的经验请用 memory_note。"
            ),
            "board_unchanged": True,
            "current_tokens": measure(getattr(state, "scratchpad", "") or ""),
            "capacity_tokens": limit,
        }

    previous = getattr(state, "scratchpad", "") or ""
    state.scratchpad = text
    state.scratchpad_revision = int(getattr(state, "scratchpad_revision", 0) or 0) + 1
    state.scratchpad_revised_turn = int(turn or 0)

    try:
        state.append_transcript(
            "whiteboard_revised",
            turn=turn,
            revision=state.scratchpad_revision,
            tokens=size,
            capacity=limit,
            unchanged=(text == previous),
        )
    except Exception:
        pass

    return {
        "status": "success",
        # 返回**板子现状 + 剩余空间**，不返回"第几条"。
        # 旧的 {"note_count": 639} 是个只涨的计数器，读起来像进展 —— 模型每写
        # 一条就收到一次 "+1" 的正反馈，这也是它停不下来的原因之一。
        "board": text,
        "tokens_used": size,
        "capacity_tokens": limit,
        "tokens_remaining": limit - size,
        "revision": state.scratchpad_revision,
        "unchanged": text == previous,
    }


def render(state: Any, *, turn: int = 0) -> str | None:
    """注入正文：板子 + 年龄。没有板子就返回 None。

    年龄只陈述事实（上次改写在第几轮、已多少轮没动），**不下判决**。判决归
    进展熔断（core/progress_breaker），证据和判决不混在一处。
    """
    board = getattr(state, "scratchpad", "") or ""
    if not board.strip():
        return None

    limit = capacity_tokens()
    size = measure(board)
    revised_turn = int(getattr(state, "scratchpad_revised_turn", 0) or 0)

    header = ["📝 **你的白板**（跨轮工作状态；下一次 write_scratchpad 会整块改写它）"]
    if revised_turn and turn > revised_turn:
        header.append(f"上次改写：turn {revised_turn}（已 {turn - revised_turn} 轮没动过）")
    elif revised_turn:
        header.append(f"上次改写：turn {revised_turn}")
    if size > limit:
        header.append(
            f"⚠️ 当前 {size} tokens，超出容量 {limit}（旧格式续连而来）—— "
            f"下一次改写必须压到容量内，否则会被拒绝。"
        )
    else:
        header.append(f"占用 {size}/{limit} tokens")

    return "\n".join(header) + "\n\n" + board.strip()


def signature(state: Any) -> int:
    """白板的进展信号 —— 改写次数。同样内容重写一遍不算改写吗？算。

    判据取"模型有没有动过板子"，不取"内容有没有变"：写入侧已经把 `unchanged`
    记进 transcript，而进展熔断真正盯的是**整轮的持久变化 + 输出复读**，
    不靠这一个信号单独定罪。
    """
    return int(getattr(state, "scratchpad_revision", 0) or 0)
