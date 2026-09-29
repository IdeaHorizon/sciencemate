"""orchestrator 对话持久化 —— chat.py 与 run_e2e_dogfood.py 共用的唯一读写实现。

根因（2026-07-08 实测事故）：两个入口各自维护一份 conversation.json 读写，
run_e2e_dogfood.py 写 **list**（裸 messages 数组），chat.py 写 **dict**（含
tokens_used / scratchpad / hook_state 元数据）且读取时假定 dict —— 用 dogfood
跑完的项目换 chat.py 续连直接 `'list' object has no attribute 'get'` 崩溃。
且 list 格式把 scratchpad / hook_state 全丢了：换入口续连后 scratchpad 为空
→ first-turn 引导重复注入 → 模型把引导文 echo 进回复，二次污染对话。

持久化格式是跨入口契约，必须单一实现。本模块是唯一合法读写点；两个入口
（以及未来任何新入口）一律 import 这里，不得自带副本。

格式：dict（schema 见 save_conversation）。load 向后兼容旧 list 格式
（只有 messages、无元数据），读到后下次 save 自动升级为 dict。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.llm import LLMMessage
from core.state import State


def conversation_path(state: State) -> Path:
    """对话存到 <orchestrator state root>/conversation.json。"""
    return state.root / "conversation.json"


def msg_to_jsonable(m: LLMMessage) -> dict:
    """LLMMessage → JSON-able dict（None 字段去掉，避免噪音）。"""
    d: dict = {"role": m.role}
    if m.content is not None:
        d["content"] = m.content
    if m.tool_calls:
        d["tool_calls"] = m.tool_calls
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    if m.name:
        d["name"] = m.name
    if m.reasoning_content is not None:
        d["reasoning_content"] = m.reasoning_content
    return d


def jsonable_to_msg(d: dict) -> LLMMessage:
    return LLMMessage(
        role=d["role"],
        content=d.get("content"),
        tool_calls=d.get("tool_calls"),
        tool_call_id=d.get("tool_call_id"),
        name=d.get("name"),
        reasoning_content=d.get("reasoning_content"),
    )


def _drop_unjsonable(d: dict) -> dict:
    """hook_state 里可能塞了不可 json 的对象（如 set / asyncio primitives）。粗略过滤。

    注意探测时**不能**给 json.dumps 传 default=str（chat.py 旧实现的隐藏 bug）：
    default=str 让 set 之类也"序列化成功"，过滤器变成永不过滤——set 被静默
    字符串化存盘（"{1, 2, 3}"），重载后类型损坏。
    """
    out = {}
    for k, v in d.items():
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            continue
    return out


def save_conversation(state: State, messages: list[LLMMessage]) -> None:
    """把 messages + state 元数据存盘（dict schema，唯一合法写入格式）。"""
    payload = {
        "run_id": state.run_id,
        "node_type": state.node_type,
        "tenant_id": state.tenant_id,
        "project_id": state.project_id,
        "session_id": state.session_id,
        "tokens_used": state.tokens_used,
        "tool_calls_made": state.tool_calls_made,
        "scratchpad": state.scratchpad,
        "scratchpad_revision": state.scratchpad_revision,
        "scratchpad_revised_turn": state.scratchpad_revised_turn,
        "hook_state": _drop_unjsonable(state.hook_state),
        "messages": [msg_to_jsonable(m) for m in messages],
    }
    path = conversation_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子替换（2026-07-09）：进程在写一半时被杀（Ctrl-C / 崩溃）不能留下截断的
    # JSON —— 那会让下次续连 load_conversation 判损坏、静默丢整段对话历史。
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                   encoding="utf-8")
    tmp.replace(path)


def checkpoint_path(state: State) -> Path:
    """agent_loop 每 turn 末写的那份（core/agent_loop._persist_messages_checkpoint）。"""
    return state.root / "messages_checkpoint.json"


def _load_checkpoint_messages(state: State) -> list[LLMMessage] | None:
    """读 turn 级 checkpoint。只有 messages，没有 scratchpad/hook_state 元数据。"""
    path = checkpoint_path(state)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    msgs = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(msgs, list) or not msgs:
        return None
    return [jsonable_to_msg(m) for m in msgs if isinstance(m, dict)]


def load_conversation(state: State) -> list[LLMMessage] | None:
    """读对话 + 恢复 state 元数据。两份都没有 / 都损坏 → None。

    兼容两种历史格式：
      - dict（标准）：恢复 tokens_used / tool_calls_made / scratchpad / hook_state
      - list（旧 dogfood 写的裸数组）：只有 messages，元数据保持 State 默认值

    ## 为什么要看两个文件（2026-08-04 实测事故）

    盘上一直有两份编排历史，写入时机不同：

      conversation.json          chat.py 在**一轮 REPL 对话结束**时写
      messages_checkpoint.json   agent_loop 在**每个 turn 末**写
                                 （docstring 原文："kill 后能从这里 resume"）

    而续连此前只读前者。于是：**第一轮 REPL 没跑完就重启 = 编排历史全丢**，
    尽管盘上躺着一份完整的 turn 级 checkpoint。e2e8 实测：一小时四十五分钟的
    编排工作没了，58 条消息的 checkpoint 就在旁边，没人读。

    这是"机制存在但没接到路径"——不是没做持久化，是写它的人和读它的人没接上。

    规则：**取更新的那份**。checkpoint 缺元数据（scratchpad/hook_state），
    所以 conversation.json 同样新时优先它；只有 checkpoint 严格更新（说明
    上一轮 REPL 之后又跑了 turn 才崩）才用 checkpoint。

    checkpoint 可能停在半截工具协议上（assistant 发了 tool_calls 但没有对应
    tool 结果）——调用方 chat.py 紧接着就跑 `_repair_message_tool_protocol`，
    那条路本来就在，不用另建。
    """
    path = conversation_path(state)
    cp_path = checkpoint_path(state)
    conv_mtime = path.stat().st_mtime if path.exists() else -1.0
    cp_mtime = cp_path.stat().st_mtime if cp_path.exists() else -1.0

    if cp_mtime > conv_mtime:
        msgs = _load_checkpoint_messages(state)
        if msgs:
            state.hook_state["_resumed_from_turn_checkpoint"] = True
            return msgs
        # checkpoint 读不出来就退回 conversation.json，不是直接放弃

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # conversation.json 损坏时 checkpoint 是唯一活路 —— 别让一个坏文件
        # 把好的那份也一起埋了
        msgs = _load_checkpoint_messages(state)
        if msgs:
            state.hook_state["_resumed_from_turn_checkpoint"] = True
        return msgs

    if isinstance(data, list):
        # 旧 dogfood 格式：裸 messages 数组，无元数据。
        return [jsonable_to_msg(m) for m in data if isinstance(m, dict)]

    if not isinstance(data, dict):
        return None

    state.tokens_used = int(data.get("tokens_used", 0) or 0)
    state.tool_calls_made = int(data.get("tool_calls_made", 0) or 0)
    # 白板从 list[str] 改成 str（2026-08-13）。**存量 checkpoint 里还是 list** ——
    # 续连时按旧格式读，笔记不丢；从此它是一块板子，模型下一次写入整体改写它。
    from core.whiteboard import adopt_legacy

    state.scratchpad = adopt_legacy(data.get("scratchpad"))
    state.scratchpad_revision = int(data.get("scratchpad_revision", 0) or 0)
    state.scratchpad_revised_turn = int(data.get("scratchpad_revised_turn", 0) or 0)
    state.hook_state.update(data.get("hook_state") or {})
    return [jsonable_to_msg(m) for m in data.get("messages", [])]
