"""Strict loader for Experiment's authoritative cross-owner registry.

Deletion predicates form a closed, mechanically evaluable language. Shell
snippets, argv, and arbitrary expressions are intentionally not part of it.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

REGISTRY_PATH = Path(__file__).resolve().parent / "cross_owner_registry.json"

_ENTRY_ID = re.compile(r"experiment-cross-owner-[0-9]{3}\Z")
_SOURCE_SYMBOL = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z",
)
_CHECK_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_OWNER_TOKEN = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_DISJUNCTIVE_TOKEN = re.compile(r"(?:^|[_-])(?:or|and)(?:[_-]|$)|[|/,+]")
_OWNER_DOMAINS = frozenset({
    "core", "platform", "experiment", "shared", "node", "run_owner",
})


class RegistryValidationError(ValueError):
    """A stable, path-addressed schema error."""

    def __init__(self, code: str, path: str, message: str):
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code} at {path}: {message}")


def _fail(code: str, path: str, message: str) -> None:
    raise RegistryValidationError(code, path, message)


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("wrong_type", path, "expected an object")
    if not all(isinstance(key, str) for key in value):
        _fail("wrong_type", path, "object keys must be strings")
    return value


def _fields(
    value: dict[str, Any],
    *,
    path: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail("unknown_fields", path, f"unknown fields: {unknown}")
    missing = sorted(required - set(value))
    if missing:
        _fail("missing_fields", path, f"missing fields: {missing}")


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _fail("wrong_type", path, "expected a string")
    if not value or value != value.strip():
        _fail("invalid_value", path, "string must be non-empty and canonically trimmed")
    return value


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail("invalid_value", path, "expected a positive integer")
    return value


def _nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("invalid_value", path, "expected a non-negative integer")
    return value


def _repo_path(value: Any, path: str) -> str:
    text = _string(value, path)
    if "\\" in text:
        _fail("invalid_path", path, "repository paths use POSIX separators")
    parsed = PurePosixPath(text)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        _fail("invalid_path", path, "expected a normalized repository-relative path")
    if str(parsed) != text:
        _fail("invalid_path", path, "path is not normalized")
    return text


def _date(value: Any, path: str) -> str:
    text = _string(value, path)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        _fail("invalid_date", path, "expected an ISO 8601 calendar date")
    if parsed.isoformat() != text:
        _fail("invalid_date", path, "date is not in canonical YYYY-MM-DD form")
    return text


def _owner_ref(value: Any, path: str) -> tuple[str, str]:
    owner = _mapping(value, path)
    _fields(owner, path=path, required={"domain", "component"})
    domain = _string(owner["domain"], f"{path}.domain")
    component = _string(owner["component"], f"{path}.component")
    if domain not in _OWNER_DOMAINS:
        _fail("invalid_owner", f"{path}.domain", f"unsupported owner domain: {domain}")
    for name, token in (("domain", domain), ("component", component)):
        if not _OWNER_TOKEN.fullmatch(token) or _DISJUNCTIVE_TOKEN.search(token):
            _fail(
                "invalid_owner",
                f"{path}.{name}",
                "owner tokens must identify one domain/component, not a disjunction",
            )
    return domain, component


def _true_owner(value: Any, path: str) -> str:
    owner = _mapping(value, path)
    state = _string(owner.get("state"), f"{path}.state")
    if state == "assigned":
        _fields(owner, path=path, required={"state", "owner"})
        _owner_ref(owner["owner"], f"{path}.owner")
        return state
    if state == "unresolved":
        _fields(
            owner,
            path=path,
            required={"state", "resolution_owner", "candidates"},
        )
        resolution_owner = _owner_ref(
            owner["resolution_owner"], f"{path}.resolution_owner",
        )
        candidates = owner["candidates"]
        if not isinstance(candidates, list) or len(candidates) < 2:
            _fail(
                "invalid_owner",
                f"{path}.candidates",
                "unresolved ownership needs at least two typed candidates",
            )
        candidate_refs = [
            _owner_ref(item, f"{path}.candidates[{index}]")
            for index, item in enumerate(candidates)
        ]
        if len(candidate_refs) != len(set(candidate_refs)):
            _fail("invalid_owner", f"{path}.candidates", "owner candidates must be unique")
        if resolution_owner not in candidate_refs:
            _fail(
                "invalid_owner",
                f"{path}.resolution_owner",
                "resolution owner must be one of the candidates",
            )
        return state
    _fail("invalid_owner", f"{path}.state", "state must be assigned or unresolved")


def _evidence(value: Any, path: str) -> None:
    evidence = _mapping(value, path)
    kind = _string(evidence.get("kind"), f"{path}.kind")
    if kind == "source":
        _fields(
            evidence,
            path=path,
            required={"kind", "path", "claim"},
            optional={"line", "line_end", "symbol"},
        )
        _repo_path(evidence["path"], f"{path}.path")
        has_line = "line" in evidence
        has_symbol = "symbol" in evidence
        if has_line == has_symbol:
            _fail(
                "invalid_evidence",
                path,
                "source evidence needs exactly one stable locator: line or symbol",
            )
        if has_line:
            line = _positive_int(evidence["line"], f"{path}.line")
        if "line_end" in evidence:
            if not has_line:
                _fail(
                    "invalid_evidence",
                    f"{path}.line_end",
                    "line_end requires a line locator",
                )
            line_end = _positive_int(evidence["line_end"], f"{path}.line_end")
            if line_end < line:
                _fail("invalid_value", f"{path}.line_end", "line_end precedes line")
        if has_symbol:
            symbol = _string(evidence["symbol"], f"{path}.symbol")
            if not _SOURCE_SYMBOL.fullmatch(symbol):
                _fail(
                    "invalid_evidence",
                    f"{path}.symbol",
                    "source symbol must be a canonical dotted Python identifier",
                )
        _string(evidence["claim"], f"{path}.claim")
        return
    if kind == "run":
        _fields(evidence, path=path, required={"kind", "path", "claim"})
        _repo_path(evidence["path"], f"{path}.path")
        _string(evidence["claim"], f"{path}.claim")
        return
    _fail("invalid_evidence_kind", f"{path}.kind", "kind must be source or run")


def _check_id(value: Any, path: str) -> str:
    check_id = _string(value, path)
    if not _CHECK_ID.fullmatch(check_id):
        _fail("invalid_check_id", path, "check id must be lowercase kebab-case")
    return check_id


def _source_match_check(check: dict[str, Any], path: str) -> None:
    _fields(
        check,
        path=path,
        required={"id", "kind", "paths", "pattern", "expect"},
    )
    paths = check["paths"]
    if not isinstance(paths, list) or not paths:
        _fail("invalid_predicate", f"{path}.paths", "paths must be a non-empty list")
    normalized_paths = [
        _repo_path(item, f"{path}.paths[{index}]")
        for index, item in enumerate(paths)
    ]
    if len(normalized_paths) != len(set(normalized_paths)):
        _fail("invalid_predicate", f"{path}.paths", "paths must be unique")
    pattern = _string(check["pattern"], f"{path}.pattern")
    try:
        re.compile(pattern)
    except re.error as exc:
        _fail("invalid_predicate", f"{path}.pattern", f"invalid regular expression: {exc}")
    expectation = _mapping(check["expect"], f"{path}.expect")
    _fields(
        expectation,
        path=f"{path}.expect",
        required={"operator", "value"},
    )
    operator = _string(expectation["operator"], f"{path}.expect.operator")
    if operator not in {"eq", "gte", "lte"}:
        _fail("invalid_predicate", f"{path}.expect.operator", "unsupported count operator")
    _nonnegative_int(expectation["value"], f"{path}.expect.value")


def _pytest_check(check: dict[str, Any], path: str) -> None:
    _fields(check, path=path, required={"id", "kind", "node_id"})
    node_id = _string(check["node_id"], f"{path}.node_id")
    if any(character.isspace() or character in "$;|&`\\" for character in node_id):
        _fail("invalid_predicate", f"{path}.node_id", "pytest node id is not canonical")
    parts = node_id.split("::")
    if len(parts) < 2 or not parts[-1].startswith("test_"):
        _fail("invalid_predicate", f"{path}.node_id", "expected a concrete pytest test node")
    test_path = _repo_path(parts[0], f"{path}.node_id")
    if not test_path.endswith(".py") or not (
        test_path.startswith("tests/")
        or test_path.startswith("nodes/") and "/tests/" in test_path
    ):
        _fail("invalid_predicate", f"{path}.node_id", "pytest node must name a test file")
    for selector in parts[1:]:
        if not re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", selector):
            _fail("invalid_predicate", f"{path}.node_id", "unsafe pytest selector")


def _deletion_condition(value: Any, path: str) -> None:
    condition = _mapping(value, path)
    _fields(condition, path=path, required={"mode", "checks"})
    if condition["mode"] != "all":
        _fail("invalid_predicate", f"{path}.mode", "only conjunction mode 'all' is allowed")
    checks = condition["checks"]
    if not isinstance(checks, list) or not checks:
        _fail("invalid_predicate", f"{path}.checks", "checks must be a non-empty list")
    seen: set[str] = set()
    for index, value in enumerate(checks):
        check_path = f"{path}.checks[{index}]"
        check = _mapping(value, check_path)
        check_id = _check_id(check.get("id"), f"{check_path}.id")
        if check_id in seen:
            _fail("duplicate_check_id", f"{check_path}.id", f"duplicate id: {check_id}")
        seen.add(check_id)
        kind = _string(check.get("kind"), f"{check_path}.kind")
        if kind == "source_match_count":
            _source_match_check(check, check_path)
        elif kind == "pytest_node":
            _pytest_check(check, check_path)
        else:
            _fail(
                "unsupported_check_kind",
                f"{check_path}.kind",
                "only source_match_count and pytest_node are allowed",
            )


def _upstream_issues(value: Any, path: str) -> None:
    if not isinstance(value, list):
        _fail("wrong_type", path, "expected a list of issue numbers")
    if not value:
        _fail(
            "invalid_value",
            path,
            "omit upstream_issues when no upstream issue has been filed",
        )
    issue_numbers = [
        _positive_int(item, f"{path}[{index}]")
        for index, item in enumerate(value)
    ]
    if len(issue_numbers) != len(set(issue_numbers)):
        _fail("invalid_value", path, "upstream issue numbers must be unique")


def _entry(value: Any, path: str) -> str:
    entry = _mapping(value, path)
    _fields(
        entry,
        path=path,
        required={
            "id", "title", "status", "symptom", "evidence", "true_owner",
            "current_node_workaround", "deletion_condition", "registered_by",
            "registered_on",
        },
        optional={"upstream_issues"},
    )
    entry_id = _string(entry["id"], f"{path}.id")
    if not _ENTRY_ID.fullmatch(entry_id):
        _fail("invalid_entry_id", f"{path}.id", "entry id is not canonical")
    _string(entry["title"], f"{path}.title")
    status = _string(entry["status"], f"{path}.status")
    if status not in {"open", "closed"}:
        _fail("invalid_status", f"{path}.status", "status must be open or closed")
    _string(entry["symptom"], f"{path}.symptom")
    evidence = entry["evidence"]
    if not isinstance(evidence, list) or not evidence:
        _fail("invalid_evidence", f"{path}.evidence", "evidence must be a non-empty list")
    for index, item in enumerate(evidence):
        _evidence(item, f"{path}.evidence[{index}]")
    owner_state = _true_owner(entry["true_owner"], f"{path}.true_owner")
    if status == "closed" and owner_state == "unresolved":
        _fail(
            "unresolved_owner_closed",
            f"{path}.status",
            "unresolved ownership must be assigned before the item can close",
        )
    _string(entry["current_node_workaround"], f"{path}.current_node_workaround")
    _deletion_condition(entry["deletion_condition"], f"{path}.deletion_condition")
    if "upstream_issues" in entry:
        _upstream_issues(entry["upstream_issues"], f"{path}.upstream_issues")
    _string(entry["registered_by"], f"{path}.registered_by")
    _date(entry["registered_on"], f"{path}.registered_on")
    return entry_id


def validate_cross_owner_registry(document: Any) -> dict[str, Any]:
    """Validate and return a registry document without weakening its shape."""
    registry = _mapping(document, "$")
    _fields(
        registry,
        path="$",
        required={"schema_version", "registry_id", "entries"},
    )
    if isinstance(registry["schema_version"], bool) or registry["schema_version"] != 1:
        _fail("unsupported_schema_version", "$.schema_version", "expected schema version 1")
    if registry["registry_id"] != "experiment_phase1_cross_owner_non_goals":
        _fail("invalid_registry_id", "$.registry_id", "unexpected registry id")
    entries = registry["entries"]
    if not isinstance(entries, list) or not entries:
        _fail("wrong_type", "$.entries", "expected a non-empty list")
    seen: set[str] = set()
    entry_ids: list[str] = []
    for index, value in enumerate(entries):
        entry_id = _entry(value, f"$.entries[{index}]")
        if entry_id in seen:
            _fail("duplicate_entry_id", f"$.entries[{index}].id", f"duplicate id: {entry_id}")
        seen.add(entry_id)
        entry_ids.append(entry_id)
    if entry_ids != sorted(entry_ids):
        _fail("entries_out_of_order", "$.entries", "entries must be sorted by id")
    return registry


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate_json_key", f"$.{key}", f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _invalid_json_constant(value: str) -> None:
    _fail("invalid_json", "$", f"non-standard JSON constant: {value}")


def load_cross_owner_registry(path: str | Path | None = None) -> dict[str, Any]:
    """Load the authoritative JSON registry with strict JSON and schema checks."""
    source = Path(path) if path is not None else REGISTRY_PATH
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        _fail("registry_unreadable", str(source), f"{type(exc).__name__}: {exc}")
    try:
        document = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_invalid_json_constant,
        )
    except RegistryValidationError:
        raise
    except json.JSONDecodeError as exc:
        _fail("invalid_json", str(source), f"line {exc.lineno}, column {exc.colno}: {exc.msg}")
    return validate_cross_owner_registry(document)


__all__ = [
    "REGISTRY_PATH",
    "RegistryValidationError",
    "load_cross_owner_registry",
    "validate_cross_owner_registry",
]
