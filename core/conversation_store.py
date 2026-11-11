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


class UnconfirmedToolOutcome(RuntimeError):
    """A saved call intent has no saved result; replaying it could repeat effects."""

    code = "unconfirmed_tool_outcome"


def unresolved_tool_calls(messages) -> dict[str, str]:
    pending = {}
    for message in messages:
        if message.role == "assistant":
            for call in message.tool_calls or []:
                pending[str(call.get("id"))] = (call.get("function") or {}).get("name", "unknown")
        elif message.role == "tool" and message.tool_call_id:
            try:
                value = json.loads(message.content or "null")
            except (ValueError, TypeError):
                value = None
            if not (isinstance(value, dict) and value.get("outcome") == "unknown"
                    and value.get("do_not_resubmit")):
                pending.pop(message.tool_call_id, None)
    return pending


def restore_recorded_tool_results(state, messages, *, turn: int,
                                  checkpoint: dict | None = None) -> None:
    """Recover confirmed results from the existing transcript, never rerun effects."""
    pending = unresolved_tool_calls(messages)
    if not pending or not state.transcript_path.is_file():
        return
    results = {}
    with state.transcript_path.open() as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            call_id = record.get("tool_call_id")
            if isinstance(checkpoint, dict) and call_id == checkpoint.get("tool_dispatch"):
                # Tool-call IDs can be reused by a provider on a later request.
                # A result before this dispatch's checkpoint cannot confirm it.
                from datetime import datetime

                try:
                    at = datetime.fromisoformat(str(record.get("at", "")).replace("Z", "+00:00"))
                    after = datetime.fromisoformat(checkpoint["written_at"].replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    continue
                if at < after:
                    continue
            if (record.get("event") == "tool_result" and call_id in pending
                    and isinstance(record.get("content"), str)):
                results[call_id] = record
    updated = list(messages)
    for call_id, record in results.items():
        actual = LLMMessage(role="tool", tool_call_id=call_id, name=record.get("name"),
                            content=record["content"])
        index = next((i for i, m in enumerate(updated)
                      if m.role == "tool" and m.tool_call_id == call_id), None)
        if index is None:
            index = next(i + 1 for i, m in enumerate(updated)
                         if any(c.get("id") == call_id for c in (m.tool_calls or [])))
            while index < len(updated) and updated[index].role == "tool":
                index += 1
            updated.insert(index, actual)
        else:
            updated[index] = actual
    render_context_change(state, messages, updated, turn=turn, source="recorded_tool_result_restored")


def retire_undispatched_calls(state, messages, *, checkpoint: dict | None,
                              selected_wait: dict | None, turn: int) -> bool:
    """Distinguish a saved plan from its one sequential in-flight operation.

    The cursor lives in messages_checkpoint, alongside the call intents/results.
    A registered blocking wait also proves later calls in its batch had not run.
    Legacy checkpoints without either fact remain conservative.
    """
    pending = unresolved_tool_calls(messages)
    planned = set()
    if isinstance(checkpoint, dict) and "tool_dispatch" in checkpoint:
        planned = set(pending) - {checkpoint["tool_dispatch"]}
    elif selected_wait and not selected_wait.get("finished"):
        wait_id = selected_wait.get("tool_call_id")
        for message in messages:
            ids = [c.get("id") for c in (message.tool_calls or [])]
            if wait_id in ids:
                planned.update(set(ids[ids.index(wait_id) + 1:]) & set(pending))
    if not planned:
        return False
    updated = list(messages)
    for call_id in planned:
        actual = LLMMessage(role="tool", tool_call_id=call_id, name=pending[call_id],
                            content=json.dumps({"status": "deferred", "executed": False,
                                "note": "Planned call was not dispatched before interruption. "
                                        "Replan from the confirmed results."}))
        index = next((i for i, m in enumerate(updated)
                      if m.role == "tool" and m.tool_call_id == call_id), None)
        if index is None:
            index = next(i + 1 for i, m in enumerate(updated)
                         if any(c.get("id") == call_id for c in (m.tool_calls or [])))
            while index < len(updated) and updated[index].role == "tool":
                index += 1
            updated.insert(index, actual)
        else:
            updated[index] = actual
    render_context_change(state, messages, updated, turn=turn, source="undispatched_calls_retired")
    return True


async def reconcile_reentrant_calls(state, messages, *, turn: int) -> None:
    """Replay only shapes declared safe by their existing tool/adapter owner."""
    from core import tool_registry
    from core.agent_loop import _persist_messages_checkpoint

    pending = unresolved_tool_calls(messages)
    for message in list(messages):
        for call in message.tool_calls or []:
            call_id = call.get("id")
            if call_id not in pending:
                continue
            function = call.get("function") or {}
            definition = tool_registry.get_tool(function.get("name"))
            if definition is None:
                continue
            raw = function.get("arguments") or "{}"
            arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
            recovery = (
                definition.interrupted_recovery(arguments, state=state, tool_call_id=call_id)
                if callable(definition.interrupted_recovery) else None
            )
            if isinstance(recovery, dict) and "result" in recovery:
                # A child/adapter owner can recover a receipt directly from its
                # existing durable facts without executing the original tool.
                result = recovery["result"]
            else:
                if not (definition.replayable_read or recovery is True):
                    continue
                state.hook_state["_current_tool_call_id_being_executed"] = call_id
                try:
                    _persist_messages_checkpoint(state, messages, turn=turn)
                    result = await tool_registry.execute(function["name"], state, **arguments)
                    if result.get("status") == "error":
                        raise UnconfirmedToolOutcome(
                            "Adapter could not reconcile the original operation: "
                            + json.dumps(result, ensure_ascii=False)
                        )
                finally:
                    state.hook_state.pop("_current_tool_call_id_being_executed", None)
            actual = LLMMessage(role="tool", tool_call_id=call_id, name=function["name"],
                                content=json.dumps(result, ensure_ascii=False))
            updated = list(messages)
            index = next((i for i, m in enumerate(updated)
                          if m.role == "tool" and m.tool_call_id == call_id), None)
            if index is None:
                index = updated.index(message) + 1
                while index < len(updated) and updated[index].role == "tool":
                    index += 1
                updated.insert(index, actual)
            else:
                updated[index] = actual
            render_context_change(state, messages, updated, turn=turn,
                                  source="adapter_operation_reconciled")
            _persist_messages_checkpoint(state, messages, turn=turn)


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
    if m.context_slot is not None:
        d["context_slot"] = m.context_slot
    d["origin"] = m.origin
    return d


def jsonable_to_msg(d: dict) -> LLMMessage:
    return LLMMessage(
        role=d["role"],
        content=d.get("content"),
        tool_calls=d.get("tool_calls"),
        tool_call_id=d.get("tool_call_id"),
        name=d.get("name"),
        reasoning_content=d.get("reasoning_content"),
        context_slot=d.get("context_slot"),
        # Missing legacy provenance is ambiguous, never inferred from text.
        origin=d.get("origin") or ("legacy_unknown" if d["role"] == "user" else None),
    )


def restore_messages(state, records: list[dict], *, through: str | None = None,
                     record_origins: bool = True) -> list[LLMMessage]:
    """The single persistence boundary for message decoding and legacy origin repair."""
    messages = [jsonable_to_msg(d) for d in records]
    if any(not d.get("origin") for d in records):
        return restore_message_origins(state, messages, through=through, record=record_origins)
    return messages  # a persisted unknown stays unknown; do not repeatedly rescan old evidence


def restore_message_origins(state, messages: list[LLMMessage], *, through: str | None = None,
                            record: bool = True) -> list[LLMMessage]:
    """Restore legacy user-message provenance from recorded append sequences.

    Seed snapshots align surviving messages; newly admitted seed entries remain
    unknown/caller-protected. Only actual framework injection events introduce
    framework authorship. Unmatched/contradictory legacy entries stay unknown.
    No text shape is an authority and no old checkpoint or log is rewritten.
    """
    if not any(m.origin == "legacy_unknown" for m in messages):
        return messages
    from dataclasses import replace
    from difflib import SequenceMatcher
    from datetime import datetime
    from collections import Counter, defaultdict
    import hashlib
    from core.llm import framework_notice

    path = getattr(state, "transcript_path", Path(state.root) / "transcript.jsonl")
    if not path.exists():
        return messages

    def users(seq):
        return [m for m in seq if m.role == "user" and not m.tool_calls and not m.tool_call_id]

    def align(previous, current):
        out = list(current)
        old_counts = Counter(m.content for m in previous)
        new_counts = Counter(m.content for m in current)
        origins = defaultdict(set)
        for msg in previous:
            origins[msg.content].add(msg.origin)
        ambiguous = {text for text, sources in origins.items()
                     if len(sources) > 1 and new_counts[text] < old_counts[text]}
        matcher = SequenceMatcher(a=[m.content for m in previous],
                                  b=[m.content for m in current], autojunk=False)
        for i, j, size in matcher.get_matching_blocks():
            for offset in range(size):
                if (out[j + offset].origin == "legacy_unknown"
                        and out[j + offset].content not in ambiguous):
                    out[j + offset] = replace(out[j + offset], origin=previous[i + offset].origin)
        return out

    try:
        cutoff = datetime.fromisoformat(through.replace("Z", "+00:00")) if through else None
    except (AttributeError, TypeError, ValueError):
        return messages
    history, evidence_hash = [], hashlib.sha256()
    try:
        with path.open(encoding="utf-8") as log:
            for line in log:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if cutoff and event.get("at"):
                    at = datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
                    if at > cutoff:
                        break
                evidence_hash.update(line.encode("utf-8"))
                kind = event.get("event")
                if kind == "loop_seed":
                    admitted = users([jsonable_to_msg(d) for d in event.get("messages", [])])
                    history = align(history, admitted)
                    # A legacy seed can include a newly admitted caller copy of
                    # identical earlier framework text. Do not certify its tail
                    # from an earlier append with the same bytes.
                    if admitted and admitted[-1].origin == "legacy_unknown":
                        history[-1] = replace(history[-1], origin="legacy_unknown")
                elif kind == "hook_injection":
                    if "messages" in event:
                        history.extend(replace(m, origin="framework") if m.origin == "legacy_unknown" else m
                                       for m in users([jsonable_to_msg(d) for d in event["messages"]]))
                    else:
                        history.extend(LLMMessage(role="user", content=text, origin="framework")
                                       for role, text in zip(event.get("roles", []), event.get("contents", []))
                                       if role == "user")
                elif kind == "framework_notice_injected":
                    history.append(framework_notice(event["text"]))
                elif kind == "user_message_injected":
                    history.extend(framework_notice(text) for text in event.get("notice_texts", []))
    except (OSError, ValueError, TypeError, KeyError):
        return messages  # unavailable evidence cannot turn unknown text into framework authority
    restored_users = iter(align(history, users(messages)))
    out = [next(restored_users) if m.role == "user" and not m.tool_calls and not m.tool_call_id else m
           for m in messages]
    if record and hasattr(state, "append_transcript"):
        state.append_transcript("message_origins_restored", through=through,
            evidence_sha256=evidence_hash.hexdigest(),
            framework_indices=[i for i, (old, new) in enumerate(zip(messages, out))
                               if old.origin == "legacy_unknown" and new.origin == "framework"],
            unknown_indices=[i for i, m in enumerate(out) if m.origin == "legacy_unknown"])
    return out


def apply_context_patch(messages: list[LLMMessage], splices: list[dict]) -> list[LLMMessage]:
    """Apply a recorded rendering delta. Runtime and transcript replay share it."""
    out = list(messages)
    for splice in splices:  # descending offsets, measured before the change
        start = splice["start"]
        out[start:start + splice["delete"]] = [jsonable_to_msg(d) for d in splice["messages"]]
    return out


def render_context_change(state, messages: list[LLMMessage], updated: list[LLMMessage],
                          *, turn: int, source: str) -> None:
    """Record only changed spans, then apply the same delta used by a replayer.

    This is a journal of a view change, not another source of task/state truth.
    Deletion, empty snapshots and rollback must be replayable too, even when no
    hook message is appended. Original transcript records remain untouched.
    """
    if len(messages) == len(updated) and all(a is b for a, b in zip(messages, updated)):
        return
    from difflib import SequenceMatcher

    before, after = ([msg_to_jsonable(m) for m in seq] for seq in (messages, updated))
    keys = lambda records: [json.dumps(d, sort_keys=True, ensure_ascii=False) for d in records]
    matcher = SequenceMatcher(a=keys(before), b=keys(after), autojunk=False)
    splices = [{"start": i, "delete": j - i, "messages": after[k:l]}
               for tag, i, j, k, l in reversed(matcher.get_opcodes()) if tag != "equal"]
    if splices:
        state.append_transcript("context_rendered", turn=turn, source=source, splices=splices)
        messages[:] = apply_context_patch(messages, splices)


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
    from core.secrets import redact
    payload = redact(payload)
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                   encoding="utf-8")
    tmp.replace(path)


def checkpoint_path(state: State) -> Path:
    """agent_loop 每 turn 末写的那份（core/agent_loop._persist_messages_checkpoint）。"""
    return state.root / "messages_checkpoint.json"


def _load_checkpoint_messages(state: State, *, record_origins: bool = True) -> list[LLMMessage] | None:
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
    return restore_messages(state, [m for m in msgs if isinstance(m, dict)],
                            through=data.get("written_at") if isinstance(data, dict) else None,
                            record_origins=record_origins)


def load_conversation(state: State, *, restore_state: bool = True) -> list[LLMMessage] | None:
    """读对话 + 恢复 state 元数据。两份都没有 / 都损坏 → None。

    `restore_state=False` 只取消息：供常驻 worker 的待命轮查看，不覆盖内存中的
    新插话、运行计数与白板。

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
        msgs = _load_checkpoint_messages(state, record_origins=restore_state)
        if msgs:
            if restore_state:
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
        msgs = _load_checkpoint_messages(state, record_origins=restore_state)
        if msgs and restore_state:
            state.hook_state["_resumed_from_turn_checkpoint"] = True
        return msgs

    if isinstance(data, list):
        # 旧 dogfood 格式：裸 messages 数组，无元数据。
        return restore_messages(state, [m for m in data if isinstance(m, dict)], record_origins=restore_state)

    if not isinstance(data, dict):
        return None

    # Standby reads must not restore an older hook_state over a live worker's
    # pending interjections, counters or scratchpad.
    if not restore_state:
        return restore_messages(state, data.get("messages", []), through=data.get("written_at"),
                                record_origins=restore_state)

    state.tokens_used = int(data.get("tokens_used", 0) or 0)
    state.tool_calls_made = int(data.get("tool_calls_made", 0) or 0)
    # 白板是一块 str 板子（2026-08-13 起）；不是 str 的值当空板。
    scratchpad = data.get("scratchpad")
    state.scratchpad = scratchpad if isinstance(scratchpad, str) else ""
    state.scratchpad_revision = int(data.get("scratchpad_revision", 0) or 0)
    state.scratchpad_revised_turn = int(data.get("scratchpad_revised_turn", 0) or 0)
    state.hook_state.update(data.get("hook_state") or {})
    return restore_messages(state, data.get("messages", []), through=data.get("written_at"),
                            record_origins=restore_state)
