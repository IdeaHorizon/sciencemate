"""Data-owned typed handoff; dispatch/retry authorization stays with the caller."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

DELIVERY_KINDS = {
    "dataset_materialized", "partial_dataset", "schema_only", "acquisition_plan",
    "needs_input", "externally_blocked", "fatal",
}

def is_structured_request(value) -> bool:
    """A dispatch label alone must not replace the original user authority."""
    return isinstance(value, dict) and (
        isinstance(value.get("preprocessing_request"), dict)
        or value.get("request_kind") in {"formal_input_preparation", "preprocessing_service_request"}
        or (value.get("request_kind") == "asset_preprocessing" and bool(value.get("required_assets")))
    )


def structured_request(state) -> dict | None:
    inputs = state.hook_state.get("node_inputs") or {}
    if not isinstance(inputs, dict):
        return None
    if "data_request_envelope" in inputs:
        raise ValueError("Use the existing structured preprocessing_request; the unused Data dispatch envelope is unsupported.")
    spec = inputs.get("spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            spec = None
    if is_structured_request(spec):
        # Tool callers can wrap the same contract in spec. Keep it structured:
        # serializing the whole object as prose mixes acceptance criteria into
        # geometry and parameter extraction.
        inputs = {**spec, **{key: value for key, value in inputs.items() if key != "spec"}}
    if is_structured_request(inputs):
        # Existing managed callers send this shape but no complete envelope.
        # Preserve their scope without claiming an attempt-bound formal receipt.
        return deepcopy(inputs)
    return None


def outcome_fields(kind: str, *, missing: list | None = None) -> dict:
    if kind not in DELIVERY_KINDS:
        raise ValueError(f"Unknown Data delivery kind: {kind}")
    return {
        "delivery_kind": kind,
        "delivery_complete": kind == "dataset_materialized",
        # Runtime/simulation readiness belongs to the consumer's own checks.
        "simulation_ready": False,
        "missing_items": deepcopy(missing or []),
    }


def artifact_name_for_attempt(state, name: str) -> str:
    # Core artifact IDs are name-based across runs. Keep every attempt's evidence.
    namespace = hashlib.sha256(f"{state.run_id}:{name}".encode()).hexdigest()[:24]
    return f"{namespace}__{name}"
