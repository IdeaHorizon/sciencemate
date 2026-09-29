"""Validate Phase-0 contract fixtures without project runtime dependencies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONTRACTS_DIR = Path(__file__).resolve().parent
NORMAL_CHOICES = ["proceed", "revise", "redirect_upstream", "abort", "edit"]
REQUIRED_SHAPES = {
    "session.message",
    "session.completed",
    "step.started",
    "step.progress",
    "step.completed",
    "step.failed",
    "tool.started",
    "tool.progress",
    "tool.retrying",
    "tool.long_running",
    "tool.completed",
    "tool.failed",
    "artifact.created",
    "artifact.versioned",
    "artifact.frozen",
    "decision.required",
    "decision.resolved",
    "usage.updated",
}


def _load_events() -> dict[Path, list[dict[str, Any]]]:
    return {
        path: [
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        ]
        for path in sorted((CONTRACTS_DIR / "fixtures").glob("*.jsonl"))
    }


def validate() -> int:
    conventions = json.loads(
        (CONTRACTS_DIR / "payload-conventions.phase0.json").read_text()
    )
    fixture_events = _load_events()
    shapes = conventions["payloadShapes"]
    forbidden = set(conventions["layout"]["forbiddenWrapperFields"])
    total = 0

    assert REQUIRED_SHAPES <= shapes.keys()
    assert conventions["layout"]["canonical"] == "direct_fields"
    assert conventions["valueConventions"]["Failure"]["required"] == [
        "errorCode",
        "errorMessage",
    ]
    assert conventions["decisionChoiceSets"]["normalPostNode"] == NORMAL_CHOICES
    assert conventions["toolIdentity"]["forbiddenInferenceInputs"] == [
        "toolName equality",
        "arguments equality",
        "wall-clock proximity",
    ]

    for path, events in fixture_events.items():
        total += len(events)
        assert [event["sequence"] for event in events] == list(
            range(1, len(events) + 1)
        )
        assert len({event["id"] for event in events}) == len(events)

        step_starts: dict[str, int] = {}
        tool_calls: dict[str, str] = {}
        for event in events:
            kind, payload = event["kind"], event["payload"]
            if kind in shapes:
                assert not forbidden.intersection(payload), (
                    f"{path}:{event['id']}: wrapper"
                )
                missing = set(shapes[kind]["required"]) - payload.keys()
                assert not missing, f"{path}:{event['id']}: missing {sorted(missing)}"
                for field, allowed in shapes[kind].get("values", {}).items():
                    assert payload[field] in allowed, f"{path}:{event['id']}:{field}"

            if kind == "step.started":
                assert payload["stepId"] not in step_starts
                step_starts[payload["stepId"]] = event["sequence"]

            if kind.startswith("tool."):
                step_id, call_id = payload.get("stepId"), payload.get("toolCallId")
                assert (
                    step_id in step_starts and step_starts[step_id] < event["sequence"]
                )
                assert call_id
                if kind == "tool.started":
                    assert call_id not in tool_calls
                    tool_calls[call_id] = step_id
                    retry_of = payload.get("retryOfToolCallId")
                    assert not retry_of or (
                        retry_of in tool_calls and retry_of != call_id
                    )
                else:
                    assert tool_calls.get(call_id) == step_id

    decision = fixture_events[CONTRACTS_DIR / "fixtures" / "decision-run.jsonl"]
    required = next(event for event in decision if event["kind"] == "decision.required")
    resolved = next(event for event in decision if event["kind"] == "decision.resolved")
    choices = [choice["choiceId"] for choice in required["payload"]["choices"]]
    assert choices == NORMAL_CHOICES
    assert required["payload"]["recommendedChoiceId"] in choices
    assert resolved["payload"]["selectedChoiceId"] in choices

    completed = fixture_events[CONTRACTS_DIR / "fixtures" / "completed-run.jsonl"]
    assert completed[-1]["kind"] == "session.completed"
    assert completed[-1]["source"]["derivedFrom"] == [completed[-2]["id"]]
    return total


if __name__ == "__main__":
    print(f"Phase-0 contracts valid: {validate()} fixture events")
