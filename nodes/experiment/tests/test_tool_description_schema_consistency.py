"""Experiment-owned model prose must agree with the live model tool surface.

The runtime surface is derived through the same harness whitelist and
``list_tools_for_node`` filter used by the agent loop. Registry source metadata
selects node-owned descriptions; shared-owner prose remains that owner's
contract and is tracked through the cross-owner registry. For every node-owned
tool, the guard scans both its top-level description and every recursive
``description`` inside its rendered parameter schema.

Parameter prose is deliberately not inferred from arbitrary neighbouring
words. Tool targets come from the full live registry and must also be visible
to Experiment. Parameter names come from the global rendered schema vocabulary,
which rejects shell/status words that merely resemble arguments. A reference is
in scope when it uses one of these forms:

* ``tool.parameter``;
* ``tool(parameter=value)`` keyword-call notation;
* ``tool `parameter``` code notation; or
* a schema-known adjacent pair at a sentence boundary, such as
  ``safe_execute_python requirements``; or
* the explicit Chinese relation ``tool 的 parameter 参数``.

Literal keyword values in call notation are also checked against rendered enum
constraints. These boundaries catch the shipped stale guidance without treating
``safe_run_bash nproc`` or ordinary phrases such as ``run_node path`` as schema
claims.
"""
from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
import re

import pytest

from core.bootstrap import bootstrap
from core.loader import load_harness
from core.tool_registry import (
    ToolDefinition,
    _REGISTRY,
    list_tools_for_node,
    to_openai_schema,
)
from nodes.experiment.tools import safe_bash as sb


_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_-]*"
_NODE_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class _Description:
    owner: str
    location: str
    text: str


@dataclass(frozen=True)
class _Reference:
    target: str
    parameter: str
    offset: int
    has_literal_value: bool = False
    literal_value: str | int | float | bool | None = None


@dataclass(frozen=True)
class _SchemaError:
    owner: str
    location: str
    target: str
    parameter: str
    reason: str

    def render(self) -> str:
        return (
            f"{self.owner} {self.location} names "
            f"{self.target}.{self.parameter}: {self.reason}"
        )


# ToolDefinition currently has no rendered result schema. These names are
# documented result fields, not model-call arguments. Keep the exception at the
# exact tool/field boundary so it cannot hide a same-named stale parameter on a
# different tool. Delete it when the registry exposes machine-readable outputs.
_KNOWN_RESULT_FIELDS = frozenset({
    ("submit_job", "job_id"),
    ("job_status", "status"),
    ("read_artifact", "path"),
})


def _runtime_tool_sets() -> tuple[dict[str, ToolDefinition], dict[str, ToolDefinition]]:
    """Return the Experiment model surface and the live registry it came from."""
    bootstrap(force=True)
    harness = load_harness("experiment")
    visible = list_tools_for_node(
        harness.node_type,
        harness.tools,
        state=None,
    )
    return {tool.name: tool for tool in visible}, dict(_REGISTRY.tools)


def _parameter_names(tool: ToolDefinition) -> frozenset[str]:
    parameters = to_openai_schema(tool)["function"]["parameters"]
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    return frozenset(properties) if isinstance(properties, dict) else frozenset()


def _known_parameter_names(
    tools: Mapping[str, ToolDefinition],
) -> frozenset[str]:
    return frozenset(
        parameter
        for tool in tools.values()
        for parameter in _parameter_names(tool)
    )


def _alternation(values: Sequence[str]) -> str:
    ordered = sorted(values, key=lambda item: (-len(item), item))
    return "|".join(re.escape(value) for value in ordered)


def _explicit_parameter_references(
    text: str,
    target_tools: Mapping[str, ToolDefinition],
    parameter_vocabulary_tools: Mapping[str, ToolDefinition],
) -> list[_Reference]:
    """Extract tool/parameter claims bounded by live registry vocabulary."""
    if not target_tools:
        return []

    known_parameters = _known_parameter_names(parameter_vocabulary_tools)
    if not known_parameters:
        return []
    tool_names = _alternation(list(target_tools))
    tool = (
        rf"(?<![A-Za-z0-9_-])`?(?P<tool>{tool_names})`?"
        rf"(?![A-Za-z0-9_-])"
    )
    patterns = (
        re.compile(rf"{tool}\.(?P<parameter>{_IDENTIFIER})(?![A-Za-z0-9_-])"),
        re.compile(rf"{tool}\s+`(?P<parameter>{_IDENTIFIER})`"),
    )

    references: set[_Reference] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            parameter = match.group("parameter")
            if parameter not in known_parameters:
                continue
            if (match.group("tool"), parameter) in _KNOWN_RESULT_FIELDS:
                continue
            references.add(
                _Reference(
                    target=match.group("tool"),
                    parameter=parameter,
                    offset=match.start(),
                )
            )

    call_pattern = re.compile(
        rf"{tool}\s*\((?P<arguments>[^()\n]{{0,1000}})\)"
    )
    for call in call_pattern.finditer(text):
        arguments = call.group("arguments")
        arguments_offset = call.start("arguments")
        try:
            parsed = ast.parse(f"_tool({arguments})", mode="eval")
        except SyntaxError:
            continue
        call_node = parsed.body
        if not isinstance(call_node, ast.Call):
            continue
        search_from = 0
        for keyword in call_node.keywords:
            parameter = keyword.arg
            if parameter is None or parameter not in known_parameters:
                continue
            parameter_offset = arguments.find(parameter, search_from)
            if parameter_offset < 0:
                parameter_offset = 0
            else:
                search_from = parameter_offset + len(parameter)
            has_literal = False
            literal: str | int | float | bool | None = None
            try:
                candidate = ast.literal_eval(keyword.value)
            except (ValueError, TypeError):
                pass
            else:
                if candidate is not Ellipsis and (
                    candidate is None
                    or isinstance(candidate, (str, int, float, bool))
                ):
                    has_literal = True
                    literal = candidate
            references.add(
                _Reference(
                    target=call.group("tool"),
                    parameter=parameter,
                    offset=arguments_offset + parameter_offset,
                    has_literal_value=has_literal,
                    literal_value=literal,
                )
            )

    # Chinese full-width call notation is common in model-facing prose. It is
    # unambiguous for a parameter-name claim even when the placeholder is not
    # valid Python and therefore cannot go through the literal-value parser.
    full_width_call_pattern = re.compile(
        rf"{tool}\s*（\s*(?P<parameter>{_IDENTIFIER})\s*="
    )
    for call in full_width_call_pattern.finditer(text):
        parameter = call.group("parameter")
        if parameter not in known_parameters:
            continue
        references.add(
            _Reference(
                target=call.group("tool"),
                parameter=parameter,
                offset=call.start(),
            )
        )

    parameter_names = _alternation(list(known_parameters))
    natural_patterns = (
        # The Chinese possessive relation remains unambiguous with or without
        # the optional “参数” marker.
        re.compile(
            rf"{tool}\s+的\s*`?(?P<parameter>{parameter_names})`?"
            rf"(?![A-Za-z0-9_-])(?:\s*参数)?"
        ),
        # A bare adjacent pair is a claim only at a sentence/line boundary;
        # this catches “safe_execute_python requirements” without turning
        # ordinary phrases such as “run_node path” or “task text” into claims.
        re.compile(
            rf"{tool}\s+(?P<parameter>{parameter_names})`?"
            rf"(?![A-Za-z0-9_-])(?:\s*参数)?"
            rf"(?=[ \t]*(?:[\n,.;:)，。；：、）】]|$))"
        ),
        # The inverse Chinese form is also a directed schema claim, e.g.
        # “requirements 参数请传给 safe_execute_python”. Keep the span short
        # and require an action relation so ordinary mentions stay out.
        re.compile(
            rf"(?P<parameter>{parameter_names})(?![A-Za-z0-9_-])\s*参数"
            rf"[^。；;\n]{{0,24}}?(?:传给|交给|使用|用)\s*{tool}"
        ),
    )
    for pattern in natural_patterns:
        for match in pattern.finditer(text):
            if (match.group("tool"), match.group("parameter")) in _KNOWN_RESULT_FIELDS:
                continue
            references.add(
                _Reference(
                    target=match.group("tool"),
                    parameter=match.group("parameter"),
                    offset=match.start(),
                )
            )

    return sorted(
        references,
        key=lambda reference: (
            reference.offset,
            reference.target,
            reference.parameter,
        ),
    )


def _hidden_tool_references(
    text: str,
    visible_tools: Mapping[str, ToolDefinition],
    registered_tools: Mapping[str, ToolDefinition],
) -> list[tuple[str, int]]:
    """Find action guidance to registered tools absent from Experiment.

    Parameterless prose has no schema reference for the normal extractor to
    validate. Restrict this check to explicit action verbs and ignore nearby
    negation, so descriptions may explain that a hidden tool must not be used
    without accidentally advertising it as a recovery path.
    """
    hidden = set(registered_tools).difference(visible_tools)
    if not hidden:
        return []
    names = _alternation(list(hidden))
    pattern = re.compile(
        rf"(?P<prefix>(?:改用|使用|用|use)\s+)"
        rf"`?(?P<tool>{names})`?(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )
    found: list[tuple[str, int]] = []
    for match in pattern.finditer(text):
        before = text[max(0, match.start() - 12):match.start()].lower()
        if re.search(r"(?:不要|不得|不能|避免|do\s+not|don't|never)\s*$", before):
            continue
        found.append((match.group("tool"), match.start("tool")))
    return found


def _iter_schema_descriptions(
    value: object,
    location: str,
) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f"{location}.{key}"
            if key == "description" and isinstance(child, str):
                yield child_location, child
            else:
                yield from _iter_schema_descriptions(child, child_location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_schema_descriptions(child, f"{location}[{index}]")


def _experiment_owned_visible_names(
    visible_tools: Mapping[str, ToolDefinition],
) -> frozenset[str]:
    """Derive the node-owned owner surface from live registry source metadata."""
    owned: set[str] = set()
    for name in visible_tools:
        source_file = _REGISTRY.tool_source_files.get(name)
        if not source_file:
            continue
        source_path = Path(source_file)
        if not source_path.is_absolute():
            source_path = _NODE_ROOT.parents[1] / source_path
        try:
            source_path.resolve().relative_to(_NODE_ROOT)
        except (OSError, ValueError):
            continue
        owned.add(name)
    return frozenset(owned)


def _model_visible_descriptions(
    visible_tools: Mapping[str, ToolDefinition],
) -> Iterator[_Description]:
    owner_names = _experiment_owned_visible_names(visible_tools)
    for owner in sorted(owner_names):
        definition = visible_tools[owner]
        function = to_openai_schema(definition)["function"]
        description = function.get("description")
        if isinstance(description, str):
            yield _Description(owner, "function.description", description)
        yield from (
            _Description(owner, location, text)
            for location, text in _iter_schema_descriptions(
                function.get("parameters"),
                "function.parameters",
            )
        )


def _description_schema_errors(
    visible_tools: Mapping[str, ToolDefinition],
    registered_tools: Mapping[str, ToolDefinition],
) -> list[_SchemaError]:
    if not visible_tools:
        return [
            _SchemaError(
                owner="<experiment>",
                location="<tool surface>",
                target="<none>",
                parameter="<none>",
                reason="the model-visible Experiment tool surface is empty",
            )
        ]

    visible_parameters = {
        name: _parameter_names(definition)
        for name, definition in visible_tools.items()
    }
    visible_property_schemas = {
        name: (
            to_openai_schema(definition)["function"]["parameters"].get(
                "properties"
            )
            or {}
        )
        for name, definition in visible_tools.items()
    }
    errors: list[_SchemaError] = []
    for description in _model_visible_descriptions(visible_tools):
        for target, _ in _hidden_tool_references(
            description.text,
            visible_tools,
            registered_tools,
        ):
            errors.append(
                _SchemaError(
                    owner=description.owner,
                    location=description.location,
                    target=target,
                    parameter="<none>",
                    reason="the target tool is registered but not model-visible to Experiment",
                )
            )
        for reference in _explicit_parameter_references(
            description.text,
            registered_tools,
            registered_tools,
        ):
            if reference.target not in visible_tools:
                reason = "the target tool is registered but not model-visible to Experiment"
            elif reference.parameter not in visible_parameters[reference.target]:
                reason = "the parameter is absent from the target's rendered schema"
            elif reference.has_literal_value:
                parameter_schema = visible_property_schemas[reference.target].get(
                    reference.parameter
                )
                enum = (
                    parameter_schema.get("enum")
                    if isinstance(parameter_schema, dict)
                    else None
                )
                if isinstance(enum, list) and reference.literal_value not in enum:
                    reason = (
                        f"literal value {reference.literal_value!r} is outside "
                        f"the rendered enum {enum!r}"
                    )
                else:
                    continue
            else:
                continue
            errors.append(
                _SchemaError(
                    owner=description.owner,
                    location=description.location,
                    target=reference.target,
                    parameter=reference.parameter,
                    reason=reason,
                )
            )
    return errors


def _stale_known_parameter(
    target: str,
    visible_tools: Mapping[str, ToolDefinition],
    registered_tools: Mapping[str, ToolDefinition],
) -> str:
    candidates = _known_parameter_names(registered_tools).difference(
        _parameter_names(visible_tools[target])
    )
    assert candidates, f"{target} unexpectedly exposes every registered parameter"
    return sorted(candidates)[0]


def _append_to_schema_descriptions(
    value: object,
    location: str,
    suffix: str,
) -> tuple[object, set[str]]:
    """Copy a schema while appending ``suffix`` to every nested description."""
    locations: set[str] = set()
    if isinstance(value, dict):
        copied: dict[str, object] = {}
        for key, child in value.items():
            child_location = f"{location}.{key}"
            if key == "description" and isinstance(child, str):
                copied[key] = child + suffix
                locations.add(child_location)
            else:
                copied_child, copied_locations = _append_to_schema_descriptions(
                    child,
                    child_location,
                    suffix,
                )
                copied[key] = copied_child
                locations.update(copied_locations)
        return copied, locations
    if isinstance(value, list):
        copied_list: list[object] = []
        for index, child in enumerate(value):
            copied_child, copied_locations = _append_to_schema_descriptions(
                child,
                f"{location}[{index}]",
                suffix,
            )
            copied_list.append(copied_child)
            locations.update(copied_locations)
        return copied_list, locations
    return value, locations


def _assert_no_errors(errors: list[_SchemaError]) -> None:
    assert not errors, "\n".join(error.render() for error in errors)


def test_model_visible_tool_descriptions_match_model_visible_schemas() -> None:
    visible, registered = _runtime_tool_sets()

    _assert_no_errors(_description_schema_errors(visible, registered))


def test_node_owned_surface_includes_critical_execution_tools() -> None:
    """Coverage may not shrink by changing the helper used by mutation tests."""
    visible, _ = _runtime_tool_sets()
    owned = _experiment_owned_visible_names(visible)

    assert {
        "fetch_resource",
        "safe_run_bash",
        "safe_execute_python",
        "submit_job",
    } <= owned


@pytest.mark.parametrize(
    ("text", "target", "parameter"),
    [
        ("submit_job.job_id", "submit_job", "job_id"),
        ("job_status.status", "job_status", "status"),
        ("read_artifact path;", "read_artifact", "path"),
    ],
)
def test_result_field_references_are_not_treated_as_input_claims(
    text: str,
    target: str,
    parameter: str,
) -> None:
    visible, registered = _runtime_tool_sets()

    assert _explicit_parameter_references(text, visible, registered) == []


def test_guard_mutates_every_node_owned_visible_top_level_description() -> None:
    visible, registered = _runtime_tool_sets()
    owner_names = _experiment_owned_visible_names(visible)
    assert owner_names
    mutated_visible = dict(visible)
    stale_by_owner: dict[str, str] = {}
    for name in owner_names:
        stale = _stale_known_parameter(name, visible, registered)
        stale_by_owner[name] = stale
        mutated_visible[name] = replace(
            visible[name],
            description=visible[name].description + f"\nMutation: {name}.{stale}",
        )
    mutated_registered = {**registered, **mutated_visible}

    errors = _description_schema_errors(mutated_visible, mutated_registered)
    caught = {
        (error.owner, error.location, error.target, error.parameter)
        for error in errors
    }
    expected = {
        (name, "function.description", name, stale_by_owner[name])
        for name in owner_names
    }

    assert expected <= caught


def test_guard_mutates_every_recursive_parameter_description() -> None:
    visible, registered = _runtime_tool_sets()
    owner_names = _experiment_owned_visible_names(visible)
    assert owner_names
    mutated_visible = dict(visible)
    expected: set[tuple[str, str, str, str]] = set()

    for name in owner_names:
        definition = visible[name]
        stale = _stale_known_parameter(name, visible, registered)
        mutated_schema, locations = _append_to_schema_descriptions(
            definition.parameters_schema,
            "function.parameters",
            f" Mutation: {name}.{stale}",
        )
        if not locations:
            continue
        assert isinstance(mutated_schema, dict)
        mutated_visible[name] = replace(
            definition,
            parameters_schema=mutated_schema,
        )
        expected.update(
            (name, location, name, stale)
            for location in locations
        )

    assert expected
    assert any(".properties." in location for _, location, _, _ in expected)
    mutated_registered = {**registered, **mutated_visible}
    errors = _description_schema_errors(mutated_visible, mutated_registered)
    caught = {
        (error.owner, error.location, error.target, error.parameter)
        for error in errors
    }

    assert expected <= caught


def test_guard_owner_scope_excludes_shared_visible_descriptions() -> None:
    visible, registered = _runtime_tool_sets()
    owner = "read_artifact"
    hidden_target = "execute_python"
    parameter = "requirements"
    assert owner in visible
    assert owner not in _experiment_owned_visible_names(visible)
    assert hidden_target in registered and hidden_target not in visible
    mutated_visible = dict(visible)
    mutated_visible[owner] = replace(
        visible[owner],
        description=(
            visible[owner].description
            + f"\nOut-of-scope mutation: {hidden_target}({parameter}=...)"
        ),
    )

    _assert_no_errors(
        _description_schema_errors(
            mutated_visible,
            {**registered, **mutated_visible},
        )
    )


def test_guard_rejects_node_guidance_to_registered_but_hidden_tools() -> None:
    visible, registered = _runtime_tool_sets()
    owner = "fetch_resource"
    hidden_name = "execute_python"
    parameter = "requirements"
    assert owner in visible
    assert hidden_name in registered and hidden_name not in visible
    assert parameter in _parameter_names(registered[hidden_name])
    mutated_visible = dict(visible)
    mutated_visible[owner] = replace(
        visible[owner],
        description=(
            visible[owner].description
            + f"\nMutation: {hidden_name}({parameter}=...)"
        ),
    )

    errors = _description_schema_errors(
        mutated_visible,
        {**registered, **mutated_visible},
    )

    assert any(
        error.owner == owner
        and error.target == hidden_name
        and error.parameter == parameter
        and "not model-visible" in error.reason
        for error in errors
    ), [error.render() for error in errors]


def test_reference_grammar_has_a_falsifiable_natural_prose_boundary() -> None:
    visible, registered = _runtime_tool_sets()
    target = next(
        name for name in sorted(visible)
        if _parameter_names(visible[name])
    )
    parameter = sorted(_parameter_names(visible[target]))[0]
    known_parameters = _known_parameter_names(registered)
    ordinary_word = "ordinarywordthatisnotaschemaparameter"
    assert ordinary_word not in known_parameters

    references = _explicit_parameter_references(
        "\n".join(
            (
                f"{target}.{parameter}",
                f"{target}({parameter}=...)",
                f"{target} `{parameter}`",
                f"安装依赖时使用受管 {target} {parameter}；",
            )
        ),
        visible,
        registered,
    )

    assert [
        (reference.target, reference.parameter)
        for reference in references
    ] == [(target, parameter)] * 4
    assert _explicit_parameter_references(
        " ".join(
            (
                f"{target} remains available.",
                f"The {parameter} value is documented separately.",
                f"Use {target} for long jobs.",
                f"使用 {target} {ordinary_word}。",
                "unknown_tool.unknown_parameter",
            )
        ),
        visible,
        registered,
    ) == []


def test_guard_rejects_stale_parameter_in_compact_routing_prose() -> None:
    """Exercise the natural-language shape that motivated this regression."""
    visible, registered = _runtime_tool_sets()
    known_parameters = _known_parameter_names(registered)
    target, stale_parameter = next(
        (name, parameter)
        for name in sorted(visible)
        for parameter in sorted(
            known_parameters.difference(_parameter_names(visible[name]))
        )
    )
    owner = sorted(_experiment_owned_visible_names(visible))[0]
    mutated_visible = dict(visible)
    mutated_visible[owner] = replace(
        visible[owner],
        description=(
            visible[owner].description
            + f"\n安装依赖时用 {target} {stale_parameter}；"
        ),
    )

    errors = _description_schema_errors(
        mutated_visible,
        {**registered, **mutated_visible},
    )

    assert any(
        error.owner == owner
        and error.target == target
        and error.parameter == stale_parameter
        and "absent" in error.reason
        for error in errors
    ), [error.render() for error in errors]


@pytest.mark.parametrize(
    "guidance",
    [
        "safe_execute_python requirements",
        "safe_execute_python 的 requirements 参数",
        "请用 safe_execute_python 的 requirements 参数安装。",
        "`safe_execute_python` 的 `requirements` 参数",
    ],
)
def test_guard_rejects_original_bug_natural_variants(guidance: str) -> None:
    visible, registered = _runtime_tool_sets()
    target = "safe_execute_python"
    parameter = "requirements"
    owner = "fetch_resource"
    assert target in visible
    assert parameter in _known_parameter_names(registered)
    assert parameter not in _parameter_names(visible[target])
    mutated_visible = dict(visible)
    mutated_visible[owner] = replace(
        visible[owner],
        description=visible[owner].description + "\nMutation: " + guidance,
    )

    errors = _description_schema_errors(
        mutated_visible,
        {**registered, **mutated_visible},
    )

    assert any(
        error.owner == owner
        and error.target == target
        and error.parameter == parameter
        and "absent" in error.reason
        for error in errors
    ), [error.render() for error in errors]


def test_reference_grammar_ignores_non_schema_shell_and_status_words() -> None:
    visible, registered = _runtime_tool_sets()

    assert _explicit_parameter_references(
        "safe_run_bash `nproc`; load_skill `feasibility-ladder`; job_status.state",
        visible,
        registered,
    ) == []


def test_guard_rejects_invalid_literal_enum_value() -> None:
    visible, registered = _runtime_tool_sets()
    owner = "fetch_resource"
    mutated_visible = dict(visible)
    mutated_visible[owner] = replace(
        visible[owner],
        description=(
            visible[owner].description
            + "\nMutation: fetch_resource(kind='svn')"
        ),
    )

    errors = _description_schema_errors(
        mutated_visible,
        {**registered, **mutated_visible},
    )

    assert any(
        error.owner == owner
        and error.target == "fetch_resource"
        and error.parameter == "kind"
        and "enum" in error.reason
        and "svn" in error.reason
        for error in errors
    ), [error.render() for error in errors]


def test_fetch_guidance_names_a_reachable_offline_install_path() -> None:
    visible, _ = _runtime_tool_sets()
    description = visible["fetch_resource"].description

    assert "fetch_resource(kind='file')" in description
    assert "safe_run_bash" in description
    assert "离线" in description
    assert "--no-index" in description
    assert "--target" in description and "--prefix" in description


@pytest.mark.parametrize(
    "destination_flag",
    ["--target /approved/deps", "--prefix=/approved/deps"],
)
def test_documented_offline_pip_install_has_a_resolved_write_target(
    destination_flag: str,
) -> None:
    _, targets, _ = sb._extract_write_targets(
        "python -m pip install --no-index "
        f"{destination_flag} /approved/acquired/package.whl",
        "/approved",
    )

    assert targets == ["/approved/deps"]
    assert sb.UNRESOLVED not in targets
