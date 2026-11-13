"""Canonical names at runtime boundaries; historical evidence stays unchanged.

Analysis was called ``hypothesis`` before the node rename. Only node and
node-wide tool/artifact identities are aliases: scientific hypotheses, claim
types and question IDs retain their meaning. See docs/artifact-model.md
for the compatibility scope and removal conditions.
"""
from __future__ import annotations

NODE_ALIASES = {"hypothesis": "analysis"}
TOOL_ALIASES = {
    "stage_hypothesis_draft": "stage_analysis_draft",
    "validate_hypothesis_outputs": "validate_analysis_outputs",
}
HOOK_ALIASES = {"hypothesis_auto_validation": "analysis_auto_validation"}
ARTIFACT_ALIASES = {
    "hypothesis_output_validation": "analysis_output_validation",
    "hypothesis_research_overview": "analysis_research_overview",
}


def canonical_node_type(value: str) -> str:
    return NODE_ALIASES.get(value, value)


_NODE_FIELDS = frozenset({
    "node_type", "nodeType", "source_node_type", "sourceNodeType", "child_node_type",
    "target_node", "source_node", "authorized_target_node", "deferred_to_node",
    "action_target_node", "redirect_target_node", "redirectTargetNode", "owner_node",
    "producing_node", "recommended_target_node", "asking_node_type",
})


def canonical_node_fields(value, *, recursive: bool = True):
    """Copy structured execution state, converting only explicit node fields.

    Run IDs, prose, artifact IDs, provenance and scientific hypothesis fields
    are intentionally not rewritten. This is an in-memory compatibility view.
    """
    if isinstance(value, list):
        return [canonical_node_fields(item) for item in value] if recursive else value
    if not isinstance(value, dict):
        return value
    return {
        key: canonical_node_type(item) if key in _NODE_FIELDS and isinstance(item, str)
        else item if not recursive or key in {
            "provenance", "amendment", "metadata", "content", "messages", "raw",
        } else canonical_node_fields(item)
        for key, item in value.items()
    }


def same_node(left: str | None, right: str | None) -> bool:
    return canonical_node_type(left or "") == canonical_node_type(right or "")


def canonical_tool_name(value: str) -> str:
    return TOOL_ALIASES.get(value, value)


def canonical_hook_name(value: str) -> str:
    return HOOK_ALIASES.get(value, value)


def canonical_artifact_type(value: str) -> str:
    return ARTIFACT_ALIASES.get(value, value)


def canonical_artifact_name(kind: str, name: str) -> str:
    """Only a renamed singleton's conventional name is a name alias."""
    canonical = canonical_artifact_type(kind)
    if canonical in ARTIFACT_ALIASES.values() and canonical_artifact_type(name) == canonical:
        return canonical
    return name


def artifact_id_aliases(value: str) -> tuple[str, ...]:
    """Equivalent lookups, preserving an existing artifact's stored ID/path."""
    kind, sep, name = value.partition("__")
    canonical = canonical_artifact_type(kind)
    if not sep or canonical not in ARTIFACT_ALIASES.values():
        return (value,)
    kinds = [canonical, *(old for old, new in ARTIFACT_ALIASES.items() if new == canonical)]
    names = [name]
    if canonical_artifact_type(name) == canonical:
        names = kinds
    return tuple(dict.fromkeys([value, *(f"{k}__{n}" for k in kinds for n in names)]))
