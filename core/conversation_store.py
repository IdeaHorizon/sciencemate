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


def write_snapshot(path: Path, payload: dict) -> None:
    """The atomic write boundary shared by messages and their child handles."""
    import os
    from core.secrets import redact

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(redact(payload), stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


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
            unfinished = isinstance(value, dict) and (
                (value.get("outcome") == "unknown" and value.get("do_not_resubmit"))
                or bool(value.get("paused_jobs"))
            )
            if not unfinished:
                pending.pop(message.tool_call_id, None)
    return pending


def dispatch_anchors(checkpoint: dict | None) -> dict[str, str]:
    if not isinstance(checkpoint, dict):
        return {}
    anchors = dict(checkpoint.get("tool_dispatches") or {})
    current = checkpoint.get("tool_dispatch")
    if current and checkpoint.get("written_at"):
        anchors.setdefault(current, checkpoint["written_at"])
    return anchors


def restore_recorded_tool_results(state, messages, *, turn: int,
                                  checkpoint: dict | None = None) -> None:
    """Recover confirmed results from the existing transcript, never rerun effects."""
    pending = unresolved_tool_calls(messages)
    if not pending or not state.transcript_path.is_file():
        return
    results = {}
    anchors = dispatch_anchors(checkpoint)
    with state.transcript_path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            call_id = record.get("tool_call_id")
            if call_id in anchors:
                # Tool-call IDs can be reused by a provider on a later request.
                # A result before this dispatch's checkpoint cannot confirm it.
                from datetime import datetime

                try:
                    at = datetime.fromisoformat(str(record.get("at", "")).replace("Z", "+00:00"))
                    after = datetime.fromisoformat(anchors[call_id].replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    continue
                if at < after:
                    continue
            if (record.get("event") in {"tool_result", "tool_result_recovered"} and call_id in pending
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
        latest = next((message for message in reversed(checkpoint.get("messages") or [])
                       if message.get("role") == "assistant"), {})
        ids = {call.get("id") for call in latest.get("tool_calls") or []}
        planned = (ids & set(pending)) - set(dispatch_anchors(checkpoint))
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


async def reconcile_reentrant_calls(state, messages, *, turn: int):
    import asyncio
    from core.child_dispatch import checkpoint

    saved = checkpoint(state)
    lock = getattr(state, "_tool_recovery_lock", None)
    if lock is None:
        lock = state._tool_recovery_lock = asyncio.Lock()
    async with lock:
        state.hook_state.pop("_unconfirmed_tool_calls", None)
        try:
            restore_recorded_tool_results(state, messages, turn=turn, checkpoint=saved)
            return await _reconcile_reentrant_calls(state, messages, turn=turn)
        finally:
            state.hook_state["_unconfirmed_tool_calls"] = unresolved_tool_calls(messages)


async def _reconcile_reentrant_calls(state, messages, *, turn: int):
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
            try:
                recovery = (
                    definition.interrupted_recovery(arguments, state=state, tool_call_id=call_id)
                    if callable(definition.interrupted_recovery) else None
                )
                import inspect
                if inspect.isawaitable(recovery):
                    recovery = await recovery
                if isinstance(recovery, dict) and "result" in recovery:
                    # A child/adapter owner can recover a receipt directly from its
                    # existing durable facts without executing the original tool.
                    result = recovery["result"]
                else:
                    if not (tool_registry.is_read_only(function["name"], arguments) or recovery is True):
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
            except Exception as exc:
                state.append_transcript("tool_recovery_observation_failed", tool_call_id=call_id,
                                        error=f"{type(exc).__name__}: {exc}")
                continue
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
            state.append_transcript("tool_result_recovered", turn=turn, name=function["name"],
                                    tool_call_id=call_id, content=actual.content, recovered=True)
            render_context_change(state, messages, updated, turn=turn,
                                  source="adapter_operation_reconciled")
            _persist_messages_checkpoint(state, messages, turn=turn)
            if result.get("status") == "pause":
                return {"call": call, "result": result}


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


def state_snapshot(state: State) -> dict:
    """One state format for turn checkpoints and completed conversations."""
    return {
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
        "hook_state": {key: value for key, value in _drop_unjsonable(state.hook_state).items()
                       if key != "_current_tool_call_id_being_executed"},
    }


def restore_snapshot(state, data: dict) -> None:
    for key in ("tokens_used", "tool_calls_made", "scratchpad_revision", "scratchpad_revised_turn"):
        if key in data:
            setattr(state, key, int(data[key] or 0))
    if "scratchpad" in data:
        state.scratchpad = data["scratchpad"] if isinstance(data["scratchpad"], str) else ""
    from core.naming import canonical_node_fields

    state.hook_state.update(canonical_node_fields(data.get("hook_state") or {}))


def save_conversation(state: State, messages: list[LLMMessage]) -> None:
    write_snapshot(conversation_path(state), {
        **state_snapshot(state), "messages": [msg_to_jsonable(m) for m in messages],
    })


def checkpoint_path(state: State) -> Path:
    """agent_loop 每 turn 末写的那份（core/agent_loop._persist_messages_checkpoint）。"""
    return state.root / "messages_checkpoint.json"


def _load_checkpoint_messages(state: State, *, record_origins: bool = True) -> list[LLMMessage] | None:
    """Read messages and, when requested, their matching saved state."""
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
    if record_origins and isinstance(data, dict):
        restore_snapshot(state, data)
    return restore_messages(state, [m for m in msgs if isinstance(m, dict)],
                            through=data.get("written_at") if isinstance(data, dict) else None,
                            record_origins=record_origins)


def load_conversation(state: State, *, restore_state: bool = True) -> list[LLMMessage] | None:
    """Load the newer atomic snapshot; legacy message-only checkpoints remain readable.

    Turn checkpoints and conversation snapshots share their state format.
    Standby readers request messages only and never replace a live worker's
    counters, pending interjections or scratchpad. A damaged file can fall back
    to the other saved snapshot.
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

    restore_snapshot(state, data)
    return restore_messages(state, data.get("messages", []), through=data.get("written_at"),
                            record_origins=restore_state)
