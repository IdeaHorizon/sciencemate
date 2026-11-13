"""A parent call keeps its child handles in its existing messages checkpoint.

This is a continuation of the original call, not another run lifecycle. The
executor binds a child before it can act; recovery enters the ordinary child
execution/finishing path with that exact handle.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.naming import canonical_node_fields, same_node


def checkpoint(state) -> dict | None:
    try:
        value = json.loads((state.root / "messages_checkpoint.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid parent messages checkpoint")
    if "child_dispatches" in value:
        value["child_dispatches"] = canonical_node_fields(value["child_dispatches"])
    return value


def _save(state, value: dict) -> None:
    from core.conversation_store import state_snapshot, write_snapshot

    value.update(state_snapshot(state))
    write_snapshot(state.root / "messages_checkpoint.json", value)
    state._child_dispatches = value.get("child_dispatches", {})


def prepare(state, dispatch: dict) -> None:
    """Save the selected execution arguments before any child is launched."""
    value = checkpoint(state)
    call_id = state.hook_state.get("_current_tool_call_id_being_executed")
    if not value or not call_id or value.get("tool_dispatch") != call_id:
        return  # direct CLI/internal calls have no suspended parent exchange
    records = value.setdefault("child_dispatches", {})
    records[dispatch["dispatch_id"]] = {**dispatch, "call_id": call_id}
    _save(state, value)


def bind(state, dispatch_id: str, child) -> None:
    """Must succeed before the child executes its first side effect."""
    value = checkpoint(state)
    if not value:
        return
    record = value.get("child_dispatches", {}).get(dispatch_id)
    if record is None:
        return
    if not same_node(record["node_type"], child.node_type):
        raise ValueError("Child handle belongs to a different node")
    previous = record.get("child_run_id")
    if previous and previous != child.run_id:
        raise ValueError("An accepted child dispatch cannot change its run")
    record["child_run_id"] = child.run_id
    _save(state, value)


def finished(state, dispatch_id: str, result: dict | None = None):
    value = checkpoint(state)
    record = (value or {}).get("child_dispatches", {}).get(dispatch_id)
    if record is None:
        return None
    if result is not None:
        record["finished"] = result
        _save(state, value)
    return record.get("finished")


async def restore_paused_child(state, call_id: str, run_id: str) -> None:
    from core import tool_registry

    saved = checkpoint(state)
    matches = [
        record
        for record in (saved or {}).get("child_dispatches", {}).values()
        if record.get("call_id") == call_id and record.get("child_run_id") == run_id
    ]
    if len(matches) != 1:
        raise ValueError("Paused child has no exact saved execution handle")
    record = matches[0]
    owner = tool_registry.get_tool(record.get("owner_tool", "run_node"))
    await owner.interrupted_recovery(
        {"_resume_dispatch_id": record["dispatch_id"]}, state=state, tool_call_id=call_id
    )


def load(state, call_id: str) -> list[dict]:
    value = checkpoint(state)
    if not value:
        return []
    from core.conversation_store import jsonable_to_msg, unresolved_tool_calls

    pending = unresolved_tool_calls(
        [jsonable_to_msg(message) for message in value.get("messages") or []]
    )
    if value.get("tool_dispatch") != call_id and call_id not in pending:
        return []
    return [
        record
        for record in value.get("child_dispatches", {}).values()
        if record.get("call_id") == call_id
    ]


async def resume_background(state) -> None:
    """Ask each existing tool owner to rejoin its detached child handles."""
    from core import tool_registry

    value = checkpoint(state)
    if not value:
        return
    state._child_dispatches = value.get("child_dispatches", {})
    for key, record in list(state._child_dispatches.items()):
        if record.get("background"):
            owner = tool_registry.get_tool(record["owner_tool"])
            if owner is None or not callable(owner.interrupted_recovery):
                raise ValueError("Background child has no execution owner")
            await owner.interrupted_recovery(
                {"_dispatch_id": key}, state=state, tool_call_id=record["call_id"]
            )


def release_background(state, run_id: str) -> None:
    value = checkpoint(state)
    if value:
        records = value.get("child_dispatches", {})
        released = [
            key
            for key, record in records.items()
            if record.get("background") and record.get("child_run_id") == run_id
        ]
        value["child_dispatches"] = {
            key: record
            for key, record in records.items()
            if not (record.get("background") and record.get("child_run_id") == run_id)
        }
        for key in released:
            state.hook_state.get("_unconfirmed_tool_calls", {}).pop("background:" + key, None)
        _save(state, value)


def unconfirmed_background(state) -> dict[str, str]:
    value = checkpoint(state)
    return {
        "background:" + key: record["node_type"]
        for key, record in (value or {}).get("child_dispatches", {}).items()
        if record.get("background") and record.get("unconfirmed")
    }


def mark_background_unconfirmed(state, dispatch_id: str) -> None:
    value = checkpoint(state)
    record = (value or {}).get("child_dispatches", {}).get(dispatch_id)
    if record is not None:
        record["unconfirmed"] = True
        state.hook_state.setdefault("_unconfirmed_tool_calls", {})["background:" + dispatch_id] = (
            record["node_type"]
        )
        _save(state, value)


def child_facts(state, record: dict) -> tuple[Path, dict | None]:
    """Inspect one bound child, never choose a child by node type or recency."""
    run_id = record["child_run_id"]
    if not isinstance(run_id, str) or Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("Invalid child run handle")
    root = state.root.parent / run_id
    if not root.is_dir():
        raise FileNotFoundError(f"Original child run is missing: {run_id}")
    terminal = None
    identity = None
    path = root / "transcript.jsonl"
    if not path.exists():
        return root, None  # bound before run_start; no child action has executed
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "run_start":
                if (
                    not same_node(event.get("node_type"), record["node_type"])
                    or event.get("session_id") != state.session_id
                ):
                    raise ValueError("Original child identity does not match its parent")
                identity = event
                terminal = None
            elif event.get("event") == "run_end":
                terminal = event
    if identity is not None:
        if (
            identity.get("parent_run_id") != state.run_id
            or identity.get("parent_dispatch_id") != record["dispatch_id"]
        ):
            raise ValueError("Original child has a different dispatch owner")
        for key in ("task_instance_uuid", "task_contract_digest"):
            expected = record["execution"].get(key)
            if expected is not None and identity.get(key) != expected:
                raise ValueError("Original child has a different task identity")
    if (root / "pause_pending.json").exists() or (terminal or {}).get("status") == "paused":
        return root, None  # executor restores the original pause, without a model turn
    if terminal is not None:
        summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
        if summary.get("run_id") != run_id or summary.get("status") != terminal.get("status"):
            raise ValueError("Original child completion receipt is inconsistent")
        return root, summary
    return root, None
