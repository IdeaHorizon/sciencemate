"""Schema normalization and deterministic quality gates for preprocessing plans."""
from __future__ import annotations

import ast
import json
import math
import re
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

from core.tool_registry import get_tool
from nodes.data.planning.request_contract import (
    caller_request_text,
    validate_preprocessing_request,
    validate_preprocessing_work_order,
)


SCHEMA_VERSION = "1.0"
ACQUISITION_CONTRACT_VERSION = "2.0"
# A download is performed in the data node only when the plan explicitly
# provides a small, actionable asset.  Unknown-size datasets deliberately
# take the document-only path so a missing estimate can never trigger a
# multi-gigabyte test download.
DEFAULT_DIRECT_DOWNLOAD_MAX_BYTES = 256 * 1024 * 1024
CRITIC_DIMENSIONS = (
    "discipline_and_solver",
    "deliverable_completeness",
    "physical_semantics",
    "tool_compatibility",
    "execution_feasibility",
    "verification_and_reproducibility",
    "safety",
)
CRITIC_WEIGHTS = {
    "discipline_and_solver": 0.15,
    "deliverable_completeness": 0.20,
    "physical_semantics": 0.20,
    "tool_compatibility": 0.15,
    "execution_feasibility": 0.15,
    "verification_and_reproducibility": 0.10,
    "safety": 0.05,
}


def _list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def canonical_filename_identity(value: Any) -> str:
    """Compare a declared file role with an on-disk filename safely.

    Analyst output may use a logical identifier (``lookup_table_a``) while an
    upstream project uses punctuation in the physical name
    (``Lookup.Table-A``).  This identity is only for matching; the original
    filename remains the value downloaded, hashed and published.
    """
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def normalize_declared_filename(value: Any) -> str:
    """Normalize a filename copied from prose without changing its identity.

    Requirement documents often place a filename inside parentheses or end a
    table-cell value with punctuation.  That punctuation is not part of the
    physical file contract and must not leak into an exact-file query/URL.
    """
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = name.strip("`'\" \t\r\n")
    return re.sub(r"[\s\]\[(){}:;,]+$", "", name)


_SOFTWARE_VERSION_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])v?\d+(?:\.\d+)+(?:\+)?(?:[-._][A-Za-z0-9]+)?"
)


def normalize_software_identity(value: Any) -> dict[str, Any]:
    """Normalize a software label at the RequirementAnalysis boundary."""
    if isinstance(value, dict):
        identity = dict(value)
        raw_name = str(
            identity.get("name")
            or identity.get("primary")
            or identity.get("application")
            or identity.get("software")
            or identity.get("solver")
            or identity.get("model")
            or ""
        ).strip()
    else:
        identity = {}
        raw_name = str(value or "").strip()
    explicit_version = str(identity.get("version") or "").strip()
    match = _SOFTWARE_VERSION_RE.search(raw_name)
    version = explicit_version or (match.group(0).lstrip("vV") if match else "")
    name = raw_name
    if match:
        name = f"{raw_name[:match.start()]} {raw_name[match.end():]}".strip(" -_/()[]")
    if name:
        identity["name"] = re.sub(r"\s+", " ", name).strip()
    if version:
        identity["version"] = version.lstrip("vV")
    return identity


def _locator_path_parts(value: Any) -> list[str]:
    """Return repository-relative path parts from a locator/path label.

    Analyst contracts sometimes carry a human label before the actual path,
    e.g. ``"release package: ungrib/Variable_Tables/Vtable.ECMWF"``.  That
    label is metadata, not a directory.  Keep this parsing in the shared
    locator normalizer so every caller (planner, executor and validator) uses
    the same repository-relative path instead of each trimming it differently.
    """
    text = str(value or "").strip()
    wrapped_path = re.search(r"\(([^()]*[\\/][^()]*)\)\s*$", text)
    if wrapped_path:
        text = wrapped_path.group(1).strip()
    # A locator may carry a prose release label before the actual
    # repository-relative path, for example ``WPS v4.4 distribution,
    # ungrib/Variable_Tables/Vtable.ECMWF``.  Extract the path-bearing suffix
    # before archive/repository-prefix cleanup; otherwise the combined label
    # is mistaken for an archive root and the first real directory
    # (``ungrib`` here) is silently dropped.
    path_matches = re.findall(
        r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+",
        text,
    )
    if path_matches:
        text = path_matches[-1]
    if ":" in text:
        prefix, suffix = text.rsplit(":", 1)
        # Do not treat a Windows drive prefix as a prose label.  URL paths are
        # parsed before this helper is called, so a colon here is a contract
        # separator rather than a URL scheme delimiter.
        if "/" in suffix and not re.fullmatch(r"[A-Za-z]", prefix.strip()):
            text = suffix.strip()
    text = text.replace("\\", "/")
    parts = [
        part for part in PurePosixPath(text).parts
        if part not in {".", "..", "/"}
    ]
    # Environment/repository-root placeholders describe the execution
    # environment, not a directory inside the repository.  Remove only
    # explicit placeholder syntax; ordinary directory names remain intact.
    while parts and re.fullmatch(
        r"(?:\$\{[^}/]+\}|\$[A-Za-z_][A-Za-z0-9_]*|<[^>/]+>|%[^%/]+%)",
        parts[0],
    ):
        parts.pop(0)
    return parts


def normalize_official_locator(value: Any) -> dict[str, str]:
    """Normalize GitHub blob/raw locators to repository/revision/path fields."""
    if isinstance(value, dict):
        result = {
            key: str(value.get(key) or "").strip()
            for key in ("repository_url", "revision", "path", "raw_url")
            if value.get(key) not in (None, "", [], {})
        }
        if result.get("raw_url") and not all(
            result.get(key) for key in ("repository_url", "revision", "path")
        ):
            result = {**normalize_official_locator(result["raw_url"]), **result}
        if result.get("path"):
            # Normalize prose/archive prefixes even when the repository or
            # revision is still missing.  Callers use this partial contract to
            # decide whether discovery is allowed; retaining the label here
            # would make a real nested path look like an archive root later.
            result["path"] = "/".join(_locator_path_parts(result["path"]))
        if result.get("repository_url") and result.get("revision") and result.get("path"):
            repository_name = result["repository_url"].rstrip("/").rsplit("/", 1)[-1]
            path_parts = _locator_path_parts(result["path"])
            # Locator paths are repository-relative.  Some upstream release
            # bundles include the repository directory itself (for example
            # ``WPS/ungrib/...``); it is a transport prefix, not a directory
            # inside the Git repository.
            if path_parts and path_parts[0].casefold() == repository_name.casefold():
                path_parts = path_parts[1:]
            # Some callers pass an archive/tree locator as the structured
            # contract path (for example ``v4.4/ungrib/...``) even though the
            # revision is already carried separately.  A GitHub raw URL has
            # its revision in its own segment, so retaining this leading
            # version would produce ``.../v4.4/v4.4/...``.  Strip only a
            # semantic-version segment; branch names such as ``master`` can
            # legitimately be part of a repository path and must remain.
            revision_value = str(result.get("revision") or "").strip()
            revision_key = re.sub(
                r"[^a-z0-9]", "", revision_value.lstrip("vV").casefold()
            )
            leading_path_key = (
                re.sub(r"[^a-z0-9]", "", path_parts[0].lstrip("vV").casefold())
                if path_parts else ""
            )
            if (
                len(path_parts) > 1
                and revision_key
                and leading_path_key == revision_key
                and re.fullmatch(r"\d+(?:\.(?:\d+))+(?:[a-z0-9._-]+)?", revision_value.lstrip("vV"), flags=re.I)
            ):
                path_parts = path_parts[1:]
            # Archive extraction roots commonly encode both repository name
            # and revision (for example project-release-v1.2).  They are not
            # part of a Git repository-relative path and must not be copied
            # into a raw-file URL.
            if len(path_parts) > 1:
                prefix_key = re.sub(r"[^a-z0-9]", "", path_parts[0].casefold())
                repository_key = re.sub(r"[^a-z0-9]", "", repository_name.casefold())
                revision_key = re.sub(
                    r"[^a-z0-9]", "", result["revision"].lstrip("vV").casefold()
                )
                if (
                    len(repository_key) >= 3
                    and repository_key in prefix_key
                    and revision_key
                    and revision_key in prefix_key
                ):
                    path_parts = path_parts[1:]
            result["path"] = "/".join(path_parts)
            owner, repository = result["repository_url"].rstrip("/").rsplit("/", 2)[-2:]
            result["raw_url"] = (
                f"https://raw.githubusercontent.com/{owner}/{repository}/"
                f"{quote(result['revision'], safe='')}/{quote(result['path'], safe='/')}"
            )
        return result
    locator = str(value or "").strip()
    if not locator:
        return {}
    parsed = urlparse(locator) if "://" in locator else None
    host = (parsed.hostname or "").casefold().removeprefix("www.") if parsed else ""
    parts = (
        [part for part in PurePosixPath(parsed.path).parts if part not in {".", "..", "/"}]
        if parsed
        else _locator_path_parts(locator)
    )
    if not parts:
        return {}
    result: dict[str, str] = {}
    if host == "github.com" and len(parts) >= 2:
        result["repository_url"] = f"https://github.com/{parts[0]}/{parts[1]}"
        if len(parts) >= 5 and parts[2].casefold() in {"blob", "raw", "tree", "commit"}:
            marker = parts[2].casefold()
            if marker == "raw" and len(parts) >= 7 and parts[3].casefold() == "refs":
                result["revision"] = parts[5]
                result["path"] = "/".join(parts[6:])
            else:
                result["revision"] = parts[3]
                result["path"] = "/".join(parts[4:])
        else:
            result["path"] = "/".join(parts[2:])
    elif host == "raw.githubusercontent.com" and len(parts) >= 4:
        result.update({
            "repository_url": f"https://github.com/{parts[0]}/{parts[1]}",
            "revision": parts[2],
            "path": "/".join(parts[3:]),
        })
    else:
        result["path"] = "/".join(parts)
    if result.get("repository_url") and result.get("revision") and result.get("path"):
        return normalize_official_locator(result)
    return result


def missing_binding_constraints(
    bindings: Any,
    content: str,
    *,
    check_categorical: bool = True,
    parameter_binding_assertions: Any = None,
    binding_mode: str = "",
    binding_source: str = "",
) -> list[str]:
    """Return structured file-bound values absent from generated text.

    Numeric bindings are compared as complete values rather than substrings.
    A unit carried by a structured key or compact value (for example
    ``resolution_km`` or ``25km``) also accepts its SI-equivalent rendering.
    Free-form explanatory prose remains generation context, not an immutable
    collection of every digit it happens to contain.
    """
    mode = str(binding_mode or "").strip().casefold()
    source = str(binding_source or "").strip().casefold()
    if source == "stage_explicit_parameters":
        check_categorical = False
        if mode != "semantic":
            parameter_binding_assertions = None

    unit_scales = {
        "km": 1000.0,
        "m": 1.0,
        "cm": 0.01,
        "mm": 0.001,
        "um": 0.000001,
        "h": 3600.0,
        "hr": 3600.0,
        "hour": 3600.0,
        "min": 60.0,
        "minute": 60.0,
        "s": 1.0,
        "sec": 1.0,
        "second": 1.0,
    }

    def key_unit(key: str) -> str:
        match = re.search(
            r"(?:^|_)(km|cm|mm|um|m|hours?|hrs?|minutes?|mins?|seconds?|secs?|s)$",
            str(key or "").lower(),
        )
        if not match:
            return ""
        return {
            "hours": "hour", "hrs": "hr", "minutes": "minute",
            "mins": "min", "seconds": "second", "secs": "sec",
        }.get(match.group(1), match.group(1))

    numeric_constraints: list[tuple[str, set[float]]] = []
    categorical_constraints: list[tuple[str, str]] = []

    def add_numeric(raw: str, unit: str = "") -> None:
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return
        alternatives = {number}
        if unit in unit_scales:
            alternatives.add(number * unit_scales[unit])
        numeric_constraints.append((str(raw), alternatives))

    def collect(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, str(child_key))
            return
        if isinstance(value, (list, tuple, set)):
            for child in value:
                collect(child, key)
            return
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            add_numeric(str(value), key_unit(key))
            return
        text = str(value).strip()
        original_text = text
        text = re.sub(
            r"(?<!\d)(?:19|20)\d{2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}(?!\d)",
            "",
            text,
        )
        text = re.sub(
            r"(?<!\d)(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日",
            "",
            text,
        )
        if not text or not re.fullmatch(r"[\s\d.+\-xX×*/,:;_%°A-Za-z]+", text):
            return
        if (
            len(original_text) <= 80
            and not re.search(r"\d", original_text)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.+\-/ ]*", original_text)
        ):
            categorical_constraints.append((f"{key}={original_text}", original_text.casefold()))
        for match in re.finditer(
            r"(?<![A-Za-z0-9_.])([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
            r"\s*(km|cm|mm|um|m|hours?|hrs?|minutes?|mins?|seconds?|secs?|s)?",
            text,
            flags=re.I,
        ):
            unit = str(match.group(2) or key_unit(key)).lower()
            unit = {
                "hours": "hour", "hrs": "hr", "minutes": "minute",
                "mins": "min", "seconds": "second", "secs": "sec",
            }.get(unit, unit)
            add_numeric(match.group(1), unit)

    def dates(value: Any) -> set[str]:
        text = str(value or "")
        matches = [
            *re.findall(r"(?<!\d)(19\d{2}|20\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)", text),
            *re.findall(r"(?<!\d)(19\d{2}|20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text),
        ]
        normalized = {
            f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            for year, month, day in matches
        }
        components: dict[str, dict[str, list[int]]] = {}
        for match in re.finditer(
            r"(?im)^\s*([A-Za-z][A-Za-z0-9_]*)_(year|month|day)\s*=\s*([^!#/\r\n]+)",
            text,
        ):
            numbers = [
                int(token)
                for token in re.findall(r"(?<![\d.])\d+(?![\d.])", match.group(3))
            ]
            if numbers:
                components.setdefault(match.group(1).lower(), {})[match.group(2).lower()] = numbers
        for parts in components.values():
            years = parts.get("year") or []
            months = parts.get("month") or []
            days = parts.get("day") or []
            for year, month, day in zip(years, months, days):
                if 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31:
                    normalized.add(f"{year:04d}-{month:02d}-{day:02d}")
        return normalized

    normalized_content = str(content or "").lower()
    # Parse rendered numeric values with the same unit semantics used for
    # bindings.  Shell/configuration artifacts commonly preserve dimensions
    # as ``25km`` or ``251x201``; requiring whitespace-delimited bare numbers
    # made valid stage contracts fail after generation.
    numeric_content = re.sub(r"(?<=\d)[x×](?=\d)", " ", normalized_content)
    content_numbers: list[float] = []
    for match in re.finditer(
        r"(?<![A-Za-z0-9_.])([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
        r"\s*(km|cm|mm|um|m|hours?|hrs?|minutes?|mins?|seconds?|secs?|s)?",
        numeric_content,
        flags=re.I,
    ):
        try:
            number = float(match.group(1))
        except (TypeError, ValueError):
            continue
        content_numbers.append(number)
        unit = str(match.group(2) or "").lower()
        unit = {
            "hours": "hour", "hrs": "hr", "minutes": "minute",
            "mins": "min", "seconds": "second", "secs": "sec",
        }.get(unit, unit)
        if unit in unit_scales:
            content_numbers.append(number * unit_scales[unit])
    collect(bindings)
    missing_values = {
        label
        for label, alternatives in numeric_constraints
        if not any(
            math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)
            for actual in content_numbers
            for expected in alternatives
        )
    }
    missing_dates = dates(bindings) - dates(normalized_content)
    normalized_identity = re.sub(r"\s+", "", normalized_content)
    missing_categories = {
        label
        for label, expected in categorical_constraints
        if check_categorical and re.sub(r"\s+", "", expected) not in normalized_identity
    }

    # A script may encode a plan value through an API or a solver-specific
    # representation (for example a symbolic level name becoming a numeric
    # request code).  Such translations must be declared by the plan rather
    # than guessed by this generic validator.  Each assertion names the
    # binding and supplies equivalent tokens; one equivalent is sufficient.
    missing_assertions: set[str] = set()
    assertions = parameter_binding_assertions
    if isinstance(assertions, dict):
        assertions = [assertions]
    if isinstance(assertions, list):
        compact_content = re.sub(r"[^a-z0-9]+", "", normalized_content)
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            if assertion.get("required") is False:
                continue
            name = str(
                assertion.get("name")
                or assertion.get("binding")
                or assertion.get("binding_path")
                or "parameter"
            ).strip()
            alternatives = assertion.get("alternatives")
            if alternatives in (None, ""):
                alternatives = assertion.get("accepted")
            if alternatives in (None, ""):
                alternatives = assertion.get("tokens")
            values = alternatives if isinstance(alternatives, list) else [alternatives]
            identities = {
                re.sub(r"[^a-z0-9]+", "", str(value).casefold())
                for value in values
                if str(value or "").strip()
            }
            if identities and not any(token in compact_content for token in identities):
                # A literal assertion may repeat a unit-bearing binding (for
                # example ``25km``).  The numeric binding check above already
                # accepts its equivalent SI representation (``25000``), so
                # do not turn that successful semantic comparison back into
                # a false missing-token error.  Non-numeric assertions remain
                # exact/declared alternatives and still require one token.
                assertion_numbers: set[float] = set()
                for value in values:
                    value_text = str(value or "")
                    for numeric in re.finditer(
                        r"(?<![A-Za-z0-9_.])([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
                        r"\s*(km|cm|mm|um|m|hours?|hrs?|minutes?|mins?|seconds?|secs?|s)?",
                        value_text,
                        flags=re.I,
                    ):
                        try:
                            number = float(numeric.group(1))
                        except (TypeError, ValueError):
                            continue
                        unit = str(numeric.group(2) or "").lower()
                        unit = {
                            "hours": "hour", "hrs": "hr", "minutes": "minute",
                            "mins": "min", "seconds": "second", "secs": "sec",
                        }.get(unit, unit)
                        assertion_numbers.add(number)
                        if unit in unit_scales:
                            assertion_numbers.add(number * unit_scales[unit])
                if assertion_numbers and any(
                    math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)
                    for actual in content_numbers
                    for expected in assertion_numbers
                ):
                    continue
                missing_assertions.add(name)
    return sorted(missing_values | missing_dates | missing_categories | missing_assertions)


def blocked_placeholder_markers(content: str) -> list[str]:
    """Return concrete placeholder/runtime-stub markers in a final artifact.

    A generated artifact is handed to another machine/stage.  A host path is
    therefore a contract violation even when it is syntactically valid: the
    writer must use the stage's relative interface path and resolve the
    workspace at runtime.  Keeping this check with the existing placeholder
    gate lets the executor and package reviewer share one rule.
    """
    text = str(content or "")
    markers: list[str] = []
    # HTTP(S)/FTP links are valid in acquisition documents; do not interpret
    # their URL path as a local filesystem path.
    text_without_urls = re.sub(
        r"(?i)\b(?:https?|ftp)://[^\s\"']+",
        lambda match: " " * len(match.group(0)),
        text,
    )
    # A shebang is an interpreter contract, not a host-specific data path.
    # Remove it only from the absolute-path scan; paths in the script body
    # remain subject to the existing portability gate.
    text_without_shebangs = re.sub(
        r"(?m)^\s*#!\s*(?:/usr/bin/env\b[^\n]*|/(?:usr/)?bin/(?:ba)?sh)\s*$",
        "",
        text_without_urls,
    )
    # POSIX pseudo-devices are portable shell interfaces, not host-specific
    # data paths.  Shell validators commonly see ``>/dev/null`` in otherwise
    # portable scripts, so remove only these standard device names from the
    # absolute-path scan while continuing to reject real filesystem paths.
    text_without_runtime_devices = re.sub(
        r"(?<![A-Za-z0-9_])/(?:dev/null|dev/std(?:in|out|err)|dev/fd/[0-9]+)(?![A-Za-z0-9_./-])",
        "",
        text_without_shebangs,
    )
    patterns = {
        "placeholder_path": r"(?i)(?:^|[\"'\s])/(?:path|your)/to/",
        "environment_absolute_path": (
            r"(?<![A-Za-z0-9_:/\.])/(?:Users|home|private|tmp|var|opt|Applications|"
            r"Library|System|Volumes|mnt|srv|workspace|root|etc|usr(?:/local)?)(?:/|$)|"
            # ``../../`` is a portable relative interface path, not a host
            # absolute path.  Require the first component after ``/`` to be
            # a real name so the generic absolute-path check cannot consume
            # parent-directory segments in generated shell scripts.
            r"(?<![A-Za-z0-9_:/\.])/(?![./*\s])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|"
            r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/])"
        ),
        "placeholder_value": r"(?i)\bPLACEHOLDER_[A-Z0-9_]+\b",
        "deferred_implementation": (
            r"(?i)placeholder\s+for\s+actual|actual\s+computation\s+placeholder|"
            r"synthetic\s*(?:/|-)\s*placeholder|placeholder\s+(?:fields?|data|computation|results?)|"
            r"\bwould\s+process\b|runtime\s+will\s+(?:execute|fill|provide)"
        ),
    }
    for name, pattern in patterns.items():
        if re.search(
            pattern,
            text_without_runtime_devices if name == "environment_absolute_path" else text,
        ):
            markers.append(name)
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        document = None

    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
        elif isinstance(value, str) and value.strip().casefold() in {
            "placeholder", "tbd", "todo", "replace_me", "changeme",
        }:
            markers.append("placeholder_value")

    inspect(document)
    if re.search(r"\.touch\s*\(\s*\)", text) and not re.search(
        r"\b(?:to_netcdf|to_csv|to_json|json\.dump|yaml\.safe_dump|write_text|write_bytes)\s*\(",
        text,
    ):
        markers.append("empty_runtime_output")
    return sorted(set(markers))


def missing_stage_interface_constraints(interface: Any, content: str) -> list[str]:
    """Check that executable text uses framework-owned hand-off paths."""
    text = str(content or "")
    invalid_paths = {
        f"forbidden_build_path:{match.group(0)}"
        for match in re.finditer(
            r"(?<![A-Za-z0-9_.-])(?:outputs/|\.data_node_work/)[^\s\"']+",
            text,
        )
    }
    if not isinstance(interface, dict):
        return sorted(invalid_paths)
    declared_paths = interface.get("required_paths")
    if isinstance(declared_paths, list):
        required_paths = [
            str(path).strip() for path in declared_paths if str(path).strip()
        ]
    else:
        required_paths = [
            str(item.get("canonical_path") or "").strip()
            for key in ("external_inputs", "upstream_runtime_inputs", "runtime_outputs")
            for item in interface.get(key) or []
            if isinstance(item, dict) and str(item.get("canonical_path") or "").strip()
        ]
    return sorted({path for path in required_paths if path not in text} | invalid_paths)


def _normalize_size_estimate(value: Any) -> dict[str, Any]:
    """Normalize a declared dataset size without guessing from its name."""
    if isinstance(value, dict):
        has_bytes = value.get("bytes") not in (None, "")
        raw_value = value.get("bytes") if has_bytes else value.get("value", value.get("amount"))
        unit = "bytes" if has_bytes else str(value.get("unit") or "unknown").strip().lower()
        basis = str(value.get("basis") or value.get("method") or "declared").strip()
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        raw_value, unit, basis = value, "bytes", "declared"
    else:
        raw_value, unit, basis = None, "unknown", "not_declared_by_caller"
    try:
        amount = float(raw_value) if raw_value not in (None, "") else None
    except (TypeError, ValueError):
        amount = None
    multipliers = {
        "b": 1, "byte": 1, "bytes": 1,
        "kb": 1000, "kib": 1024,
        "mb": 1000 ** 2, "mib": 1024 ** 2,
        "gb": 1000 ** 3, "gib": 1024 ** 3,
        "tb": 1000 ** 4, "tib": 1024 ** 4,
    }
    bytes_value = int(amount * multipliers[unit]) if amount is not None and unit in multipliers and amount >= 0 else None
    result = {"value": raw_value, "unit": unit, "basis": basis}
    if bytes_value is not None:
        result["bytes"] = bytes_value
    return result


def _acquisition_source_ready(retrieval: dict[str, Any], source_status: str = "") -> bool:
    """Whether a contract has enough source information to skip discovery."""
    if source_status in {"no_result", "unresolved", "deferred_dependency", "search_error"}:
        return False
    if re.match(r"^https?://", str(retrieval.get("locator") or ""), flags=re.I):
        return True
    request = retrieval.get("request_parameters") or retrieval.get("request")
    if isinstance(request, dict) and request.get("requirements_status") == "not_fully_declared":
        return False
    if retrieval.get("endpoint") or retrieval.get("request_template"):
        return bool(request or retrieval.get("dataset_identifier"))
    return bool(
        retrieval.get("provider")
        and retrieval.get("dataset_identifier")
        and request
    )


def _step_request_parameters(source: dict[str, Any], retrieval: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Keep data-request dimensions while excluding unrelated stage context."""
    explicit = (
        retrieval.get("request_parameters") or retrieval.get("request")
        or source.get("request_parameters") or source.get("request")
    )
    if isinstance(explicit, dict) and explicit:
        return deepcopy(explicit)
    stage = source.get("available_stage_parameters")
    if not isinstance(stage, dict):
        return {}
    dimensions = {
        "dataset", "dataset_id", "dataset_identifier", "product", "version", "variables", "variable",
        "levels", "level", "pressure_levels", "model_levels", "time", "period", "date", "time_range",
        "start", "end", "spatial", "domain", "bbox", "region", "grid", "resolution", "format",
        "frequency", "forecast_hour", "member", "ensemble", "tile", "chunk", "partition",
    }
    selected = {
        str(key): deepcopy(value)
        for key, value in stage.items()
        if str(key).casefold().replace("-", "_") in dimensions
        or any(token in str(key).casefold().replace("-", "_") for token in ("dataset", "variable", "level", "time", "date", "space", "domain", "region", "grid", "resolution", "format", "product", "version"))
    }
    return selected


def normalize_acquisition_contract(value: Any) -> dict[str, Any]:
    """Return the framework-owned, step-level data preparation contract.

    The contract describes how a downstream node obtains the data.  It does
    not require a URL: unresolved web discovery is represented explicitly and
    still produces a useful acquisition document.  Only a declared small
    payload with an actionable source is eligible for direct download.
    """
    source = dict(value) if isinstance(value, dict) else {}
    nested = source.get("acquisition_contract")
    contract = deepcopy(nested) if isinstance(nested, dict) else {}
    context = source.get("acquisition_context")
    if isinstance(context, dict):
        source = {**context, **source}
    source_details = source.get("source")
    source_details = source_details if isinstance(source_details, dict) else {}

    identity = contract.get("dataset_identity") or source.get("dataset_identity")
    identity = dict(identity) if isinstance(identity, dict) else {}
    asset_id = str(
        identity.get("id") or source.get("id") or source.get("logical_asset_id")
        or source.get("acquisition_id") or ""
    ).strip()
    name = str(
        identity.get("name") or identity.get("dataset") or source.get("name")
        or source.get("name_or_role") or source_details.get("dataset")
        or source.get("scientific_role") or asset_id or ""
    ).strip()
    representation = str(
        identity.get("representation") or source.get("representation")
        or source.get("format") or ""
    ).strip()
    identity.update({
        key: item for key, item in {
            "id": asset_id,
            "name": name,
            "scientific_role": source.get("scientific_role") or identity.get("scientific_role"),
            "representation": representation,
            "format": source.get("format") or identity.get("format"),
        }.items() if item not in (None, "", [], {})
    })

    retrieval = contract.get("retrieval_instructions") or source.get("retrieval_instructions")
    retrieval = dict(retrieval) if isinstance(retrieval, dict) else {}
    # Earlier Designer drafts used a flat acquisition_contract.  Normalize it
    # here rather than silently discarding its method and locator.
    for key in (
        "method", "access_method", "locator", "locator_kind", "url", "endpoint",
        "request_template", "request", "request_parameters", "command", "provider",
        "dataset_identifier",
    ):
        if retrieval.get(key) in (None, "", [], {}) and contract.get(key) not in (None, "", [], {}):
            retrieval[key] = deepcopy(contract[key])
    method = str(
        retrieval.get("method") or retrieval.get("access_method")
        or source.get("access_method") or source_details.get("access_method")
        or source.get("source_strategy") or source.get("acquisition_kind") or ""
    ).strip()
    locator_fields = (
        "locator", "url", "endpoint", "api", "request_template", "request",
        "command", "source_location",
    )
    locator = next(
        (
            str(container.get(key)).strip()
            for container in (retrieval, source, source_details)
            for key in locator_fields
            if container.get(key) not in (None, "", [], {})
        ),
        "",
    )
    locator_kind = str(retrieval.get("locator_kind") or "").strip()
    request_parameters = _step_request_parameters(source, retrieval, contract)
    approved_source_evidence = []
    for candidate in (
        retrieval.get("approved_source_evidence"),
        source.get("evidence"),
    ):
        values = candidate if isinstance(candidate, list) else [candidate]
        for evidence_item in values:
            if evidence_item not in (None, "", [], {}) and evidence_item not in approved_source_evidence:
                approved_source_evidence.append(deepcopy(evidence_item))
    retrieval.update({
        key: item for key, item in {
            "method": method,
            "locator": locator,
            # Keep an explicit URL alias for human-facing acquisition
            # documents.  ``locator`` remains the canonical field used by
            # routing/validation, while ``url`` makes the navigable source
            # visible without requiring consumers to know that alias.
            "url": (
                retrieval.get("url") or locator
                if re.match(r"https?://", locator, flags=re.I)
                else retrieval.get("url")
            ),
            "locator_kind": locator_kind or (
                "url" if re.match(r"https?://", locator) else "declared_source" if locator else ""
            ),
            "provider": retrieval.get("provider") or source.get("provider") or source_details.get("provider"),
            "dataset_identifier": (
                retrieval.get("dataset_identifier") or source.get("dataset_identifier")
            ),
            "request_parameters": request_parameters,
            "source_strategy": source.get("source_strategy"),
            "acquisition_kind": source.get("acquisition_kind"),
            "approved_source_evidence": approved_source_evidence,
        }.items() if item not in (None, "", [], {})
    })
    reference_status = str(
        contract.get("reference_status") or source.get("reference_status") or ""
    ).strip().lower()
    if reference_status in {"no_result", "discovered_candidate", "deferred_dependency", "execution_error", "unusable_candidate", "candidate_invalid"}:
        # The acquisition manifest is still a valid deliverable when a
        # search produced no usable locator. Preserve the deferred state so
        # validation does not confuse a pending dependency with a resolved
        # download.
        retrieval["status"] = reference_status
    if retrieval.get("dataset_identifier") == identity.get("id"):
        # A logical asset id is not an upstream provider dataset id.
        retrieval.pop("dataset_identifier", None)

    stage_id = str(source.get("stage_id") or source.get("owner_stage") or "").strip()
    expected = contract.get("expected_output") or source.get("expected_output")
    expected = dict(expected) if isinstance(expected, dict) else {}
    if not expected.get("target_path") and expected.get("canonical_path"):
        expected["target_path"] = expected["canonical_path"]
    declared_representation = source.get("expected_representation")
    if isinstance(declared_representation, dict):
        expected.setdefault(
            "representation",
            declared_representation.get("representation")
            or declared_representation.get("format")
            or representation,
        )
    else:
        expected.setdefault("representation", declared_representation or representation)
    if not expected.get("target_path") and stage_id and asset_id:
        expected["target_path"] = f"runtime_inputs/{stage_id}/{asset_id}"

    validation = contract.get("validation") or source.get("validation") or source.get("acceptance_criteria") or [
        "Verify the retrieved dataset matches the declared representation and scientific coverage.",
        "Record source identity and checksum before downstream execution.",
    ]
    downstream = (
        contract.get("downstream_resume") or contract.get("downstream_resume_instructions")
        or source.get("downstream_resume") or source.get("downstream_resume_instructions")
        or source.get("resume_contract")
    )
    if downstream in (None, "", [], {}) and expected.get("target_path"):
        downstream = (
            f"Retrieve and validate the dataset, place it at {expected['target_path']}, "
            "then resume the owning stage."
        )
    size_source = (
        contract.get("size_estimate") or contract.get("estimated_size")
        or source.get("size_estimate") or source.get("estimated_size")
        or source.get("estimated_size_bytes") or source.get("size_bytes")
    )
    size_estimate = _normalize_size_estimate(size_source)
    source_discovery = contract.get("source_discovery") or source.get("source_discovery") or {}
    if not isinstance(source_discovery, dict):
        source_discovery = {}
    source_discovery = {
        **source_discovery,
        "status": str(
            source_discovery.get("status")
            or (
                "no_result" if reference_status == "no_result"
                else "discovered_candidate" if reference_status == "discovered_candidate"
                else "unresolved" if reference_status in {"deferred_dependency", "execution_error"}
                else "resolved" if _acquisition_source_ready(retrieval, reference_status)
                else "unresolved"
            )
        ).strip().lower(),
    }
    for key in ("reason", "query", "providers", "attempted_sources", "candidate_count"):
        if key not in source_discovery and source.get(key) not in (None, "", [], {}):
            source_discovery[key] = deepcopy(source[key])
    if source_discovery["status"] in {"unresolved", "no_result", "discovered_candidate", "deferred_dependency", "execution_error", "unusable_candidate", "candidate_invalid", "search_error"}:
        source_discovery.setdefault(
            "reason",
            "No usable source has been confirmed yet; use the documented request contract when an approved source becomes available.",
        )
    retrieval_ready = _acquisition_source_ready(retrieval, source_discovery["status"])
    explicit_mode = str(
        contract.get("delivery_mode") or source.get("delivery_mode") or ""
    ).strip().lower()
    has_direct_source = bool(re.match(r"^https?://", str(retrieval.get("url") or retrieval.get("locator") or ""), flags=re.I))
    small_enough = bool(size_estimate.get("bytes") is not None and size_estimate["bytes"] <= DEFAULT_DIRECT_DOWNLOAD_MAX_BYTES)
    if explicit_mode in {"direct_download", "acquisition_plan"}:
        delivery_mode = explicit_mode
    elif has_direct_source and small_enough:
        delivery_mode = "direct_download"
    else:
        delivery_mode = "acquisition_plan"
    if delivery_mode == "direct_download" and not (has_direct_source and small_enough):
        delivery_mode = "acquisition_plan"
    request = retrieval.get("request") or retrieval.get("request_parameters") or {}
    if not isinstance(request, dict):
        request = {"description": str(request)} if request else {}
    if not request:
        request = {
            "requirements_status": "not_fully_declared",
            "missing_dimensions": [
                "dataset_or_product_version",
                "time_period_or_forecast_window",
                "spatial_domain_or_grid",
                "variables_levels_or_product_fields",
            ],
            "basis": "The Research Plan or approved source did not expose these dimensions; fill them before retrieval.",
        }
    workflow = contract.get("download_workflow") or source.get("download_workflow") or []
    if isinstance(workflow, str):
        workflow = [workflow]
    if not isinstance(workflow, list):
        workflow = []
    if not workflow:
        if delivery_mode == "direct_download":
            workflow = [
                "Download the declared small asset from the approved source.",
                "Store it at expected_output.target_path and record its SHA-256 hash.",
                "Run the validation checks before handing it to the owning step.",
            ]
        else:
            workflow = [
                "Confirm access credentials and the source/version recorded in this document.",
                "Submit the request using the complete request_parameters, splitting by time, space, or product where necessary.",
                "Download in resumable chunks without executing the scientific workflow.",
                "Validate format, coverage, dimensions, and checksum, then place the result at expected_output.target_path.",
                "Resume the owning stage only after the validation record is complete.",
            ]
    retrieval["request_parameters"] = request
    retrieval["source_status"] = source_discovery["status"]
    return {
        "schema_version": ACQUISITION_CONTRACT_VERSION,
        "delivery_mode": delivery_mode,
        "size_estimate": size_estimate,
        "source_discovery": source_discovery,
        "dataset_identity": identity,
        "retrieval_instructions": retrieval,
        "download_workflow": [str(item).strip() for item in workflow if str(item).strip()],
        "expected_output": expected,
        "validation": validation,
        "downstream_resume": downstream,
        "source_ready": retrieval_ready,
    }


def normalize_artifact_spec(value: Any) -> dict[str, Any]:
    """Normalize the writer's compact, framework-owned artifact contract.

    Designer may emit a single source/evidence/criterion while the executor
    consumes arrays.  Evidence is additionally represented as provenance
    objects so schema validation and the Critic see the same contract.
    """
    spec = dict(value) if isinstance(value, dict) else {}
    sources = spec.get("parameter_sources")
    if sources not in (None, "", [], {}):
        values = sources if isinstance(sources, list) else [sources]
        spec["parameter_sources"] = [
            str(item).strip() for item in values if str(item).strip()
        ]
    evidence = spec.get("evidence")
    if evidence not in (None, "", [], {}):
        values = evidence if isinstance(evidence, list) else [evidence]
        spec["evidence"] = [
            item if isinstance(item, dict) else {
                "source_type": "declared",
                "detail": str(item).strip(),
            }
            for item in values
            if isinstance(item, dict) or str(item).strip()
        ]
    criteria = spec.get("acceptance_criteria")
    if criteria not in (None, "", [], {}):
        values = criteria if isinstance(criteria, list) else [criteria]
        spec["acceptance_criteria"] = [
            str(item).strip() for item in values if str(item).strip()
        ]
    dependencies = spec.get("runtime_dependencies")
    if dependencies not in (None, "", [], {}):
        values = dependencies if isinstance(dependencies, list) else [dependencies]
        normalized: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = str(item).strip()
            key = re.split(r"[<>=!~;\s\[]", text, maxsplit=1)[0].replace("_", "-").casefold()
            if text and key not in seen:
                seen.add(key)
                normalized.append(text)
        spec["runtime_dependencies"] = normalized
    return spec


def acquisition_contract_errors(value: Any) -> list[str]:
    contract = normalize_acquisition_contract(value)
    missing: list[str] = []
    if contract.get("schema_version") != ACQUISITION_CONTRACT_VERSION:
        missing.append("schema_version")
    if not contract["dataset_identity"].get("name"):
        missing.append("dataset_identity")
    retrieval = contract["retrieval_instructions"]
    if not retrieval.get("method"):
        missing.append("retrieval_instructions")
    if contract.get("delivery_mode") not in {"direct_download", "acquisition_plan"}:
        missing.append("delivery_mode")
    if not isinstance(contract.get("size_estimate"), dict):
        missing.append("size_estimate")
    discovery = contract.get("source_discovery")
    if not isinstance(discovery, dict) or not str(discovery.get("status") or "").strip():
        missing.append("source_discovery")
    elif str(discovery.get("status") or "").strip().lower() in {
        "unresolved", "no_result", "deferred_dependency", "search_error",
    } and not str(discovery.get("reason") or "").strip():
        missing.append("source_discovery.reason")
    workflow = contract.get("download_workflow")
    if not isinstance(workflow, list) or not any(str(item).strip() for item in workflow):
        missing.append("download_workflow")
    locator = str(retrieval.get("locator") or retrieval.get("url") or "").strip()
    request_template = retrieval.get("request_template") or retrieval.get("request") or retrieval.get("request_parameters")
    source_status = str(discovery.get("status") or "").strip().lower() if isinstance(discovery, dict) else ""
    if contract.get("delivery_mode") == "direct_download":
        if not re.match(r"^https?://", locator, flags=re.I):
            missing.append("direct_download.source_url")
        if not contract.get("size_estimate", {}).get("bytes"):
            missing.append("direct_download.size_estimate")
    elif source_status not in {"unresolved", "no_result", "discovered_candidate", "deferred_dependency", "search_error", "candidate_invalid", "unusable_candidate"}:
        # A resolved acquisition plan may use a portal/API/provider contract;
        # a URL is only one possible source description, never the contract.
        if not (
            locator
            or retrieval.get("endpoint")
            or retrieval.get("provider") and retrieval.get("dataset_identifier")
            or request_template
        ):
            missing.append("source_or_request_contract")
    method = str(retrieval.get("method") or "").strip().lower()
    if re.search(r"\bapi\b", method) and not (
        retrieval.get("request_template") or retrieval.get("request")
        or retrieval.get("request_parameters")
    ):
        missing.append("request_parameters")
    if not contract["expected_output"].get("representation"):
        missing.append("expected_representation")
    if not contract["expected_output"].get("target_path"):
        missing.append("expected_output")
    if contract["validation"] in (None, "", [], {}):
        missing.append("validation")
    if contract["downstream_resume"] in (None, "", [], {}):
        missing.append("downstream_resume")
    return sorted(set(missing))


def acquisition_contract_consistency_errors(
    value: Any,
    consumer_bindings: Any,
    *,
    semantic: bool = False,
) -> list[str]:
    """Check that a consumer's declared request has a matching acquisition contract.

    This gate compares structured fields, not application names or free-form
    prose.  A script may translate values into an API/solver encoding when
    ``semantic`` is true, but the acquisition document must still expose the
    corresponding request dimensions.  Generic dimensional contradictions
    (for example model-level data paired with a single-level source) remain
    errors regardless of the encoding.
    """
    if not isinstance(consumer_bindings, dict) or not consumer_bindings:
        return []
    contract = normalize_acquisition_contract(value)
    retrieval = contract.get("retrieval_instructions") or {}
    request = retrieval.get("request_parameters")
    if not isinstance(request, dict):
        request = {}
    relevant_keys = {
        "dataset", "datasets", "variables", "variable", "levels", "level",
        "pressure_level", "pressure_levels", "grid", "period", "date",
        "time", "format", "resolution", "spatial_resolution", "domain",
        "representation",
    }
    aliases = {
        "dataset": {"dataset", "datasets", "dataset_identifier"},
        "variables": {"variables", "variable", "params", "param"},
        "pressure_level": {"pressure_level", "pressure_levels", "levels", "level", "levelist", "levtype"},
        "grid": {"grid", "resolution", "spatial_resolution"},
        "period": {"period", "date", "dates", "time_range"},
        "time": {"time", "temporal_resolution"},
        "domain": {"domain", "region", "bbox"},
    }
    errors: list[str] = []
    for raw_key, raw_value in consumer_bindings.items():
        key = re.sub(r"[^a-z0-9]+", "_", str(raw_key).casefold()).strip("_")
        if key not in relevant_keys or key in {"format", "representation"} or raw_value in (None, "", [], {}):
            continue
        candidate_keys = aliases.get(key, {key})
        if not any(request.get(candidate) not in (None, "", [], {}) for candidate in candidate_keys):
            errors.append(f"request_parameters missing {raw_key}")

    target_text = json.dumps(consumer_bindings, ensure_ascii=False).casefold()
    source_text = " ".join(
        str(value)
        for value in (
            retrieval.get("locator"),
            retrieval.get("dataset_identifier"),
            retrieval.get("request_template"),
            json.dumps(request, ensure_ascii=False),
        )
        if value not in (None, "", [], {})
    ).casefold()
    if acquisition_dimension_conflict(target_text, source_text):
        errors.append("acquisition source contradicts declared level representation")
    if semantic:
        # Semantic encodings are intentionally not compared token-for-token.
        # The adapter/assertion contract validates the translation at the file
        # boundary; this gate only checks the acquisition dimensions above.
        return sorted(set(errors))
    return sorted(set(errors))


def acquisition_dimension_conflict(target: Any, source: Any) -> bool:
    """Return whether two structured acquisition descriptions disagree on levels."""
    target_text = str(target or "").casefold()
    source_text = str(source or "").casefold()
    target_model = bool(re.search(r"(?<![a-z0-9])(?:model[_ -]?levels?|pressure[_ -]?levels?)(?![a-z0-9])", target_text))
    target_single = bool(re.search(r"(?<![a-z0-9])(?:single[_ -]?levels?|surface[_ -]?levels?)(?![a-z0-9])", target_text))
    source_model = bool(re.search(r"(?<![a-z0-9])(?:model[_ -]?levels?|pressure[_ -]?levels?)(?![a-z0-9])", source_text))
    source_single = bool(re.search(r"(?<![a-z0-9])(?:single[_ -]?levels?|surface[_ -]?levels?)(?![a-z0-9])", source_text))
    # A single acquisition request may intentionally cover both pressure/model
    # and single/surface levels (ERA5 is a common example).  The presence of
    # the other level family is not a contradiction in that mixed contract;
    # only two *exclusive* level declarations should block the plan.
    if (target_model and target_single) or (source_model and source_single):
        return False
    return (target_model and source_single) or (target_single and source_model)


def serialize_acquisition_contract(value: Any) -> str:
    contract = normalize_acquisition_contract(value)

    def portable_provenance(item: Any, key: str = "", in_source: bool = False) -> Any:
        """Keep provenance portable without changing actionable paths.

        Acquisition documents may carry the absolute path of an upstream
        research-plan file as provenance.  That path is metadata, not a
        runtime input, so publishing it verbatim would trip the generic
        artifact portability gate on every machine.  Keep the source identity
        while exposing only a stable input URI; declared target/locator paths
        remain untouched and are still validated normally.
        """
        source_context = in_source or key.casefold() == "source"
        if isinstance(item, dict):
            return {
                name: portable_provenance(child, str(name), source_context)
                for name, child in item.items()
            }
        if isinstance(item, list):
            return [portable_provenance(child, key, source_context) for child in item]
        if source_context and isinstance(item, str):
            normalized = item.replace("\\", "/")
            if re.match(r"^(?:/|[A-Za-z]:/|//)", normalized) and not re.match(
                r"(?i)^(?:https?|ftp)://", normalized
            ):
                return "input://" + PurePosixPath(normalized).name
        return item

    return json.dumps(portable_provenance(contract), indent=2, ensure_ascii=False)


def missing_acquisition_contract_sections(content: str) -> list[str]:
    """Validate a serialized acquisition contract through canonical aliases."""
    try:
        document = json.loads(str(content or ""))
    except json.JSONDecodeError:
        return ["valid_json"]
    if not isinstance(document, dict):
        return ["json_object"]
    missing = acquisition_contract_errors(document)
    # Validate portability on the canonical acquisition representation.  A
    # local absolute path inside provenance is metadata and is normalized to
    # ``input://...``; actionable locator/target paths remain unchanged.
    portable_content = serialize_acquisition_contract(document)
    if blocked_placeholder_markers(portable_content):
        missing.append("placeholder_free")
    return sorted(set(missing))


LOCAL_ASSET_STRATEGIES = {"local_generation", "generated", "generated_artifact"}
LOCAL_REUSE_STRATEGIES = {"local_reuse", "local_asset", "local_asset_reuse"}
EXTERNAL_ASSET_ROLES = {
    "official_file_reference", "external_asset", "reference_asset",
    "external_dataset_reference", "restricted_asset_reference",
}
EXTERNAL_ASSET_STRATEGIES = {
    "official_repository", "external_source", "external_download", "download_required",
}
EXTERNAL_ACQUISITION_KINDS = {
    "official_file_reference", "external_download", "download_required", "external_dataset",
    "public_dataset", "dataset",
}
LOCAL_WORKFLOW_CAPABILITIES = {
    "local_artifact_generation", "configuration_generation", "preprocessing_script_generation",
    "local_asset_reuse",
}
EXTERNAL_WORKFLOW_CAPABILITIES = {
    "dataset_acquisition", "official_file_acquisition", "geometry_acquisition",
    "mesh_generation", "atomic_structure_generation",
}
RUNTIME_ACCESS_SCIENTIFIC_ROLES = {
    "authentication_credentials", "access_credential", "access_credentials",
    "license_entitlement", "account_access", "authorization_token",
}
RUNTIME_TOOL_SCIENTIFIC_ROLES = {
    "software_executable", "runtime_tool", "compiler", "interpreter",
    "solver_executable", "decoder_executable", "interpolator_executable",
}


def _structured_role_tokens(*values: Any) -> set[str]:
    """Tokenize structured contract fields without substring routing.

    Scientific roles commonly use snake/kebab case.  Exact tokens keep
    ``executable`` distinct from ``table`` (the latter is a suffix of the
    former) while still allowing open scientific names such as
    ``atmospheric_model_executable`` to map onto the closed runtime contract.
    """
    return {
        token.casefold()
        for value in values
        for token in re.findall(r"[A-Za-z0-9]+", str(value or ""))
        if token
    }

# TaskScopeContract deliberately uses a small, closed routing vocabulary.  A
# requirement analyst may describe domain operations in ``declared_operations``
# (for example ``sst_perturbation`` or ``namelist_generation``), but those
# strings are scientific semantics and never become executor routes by
# themselves.
ROUTING_CAPABILITIES = frozenset(
    LOCAL_WORKFLOW_CAPABILITIES
    | EXTERNAL_WORKFLOW_CAPABILITIES
)

PREFLIGHT_TOOL_NAMES = frozenset({
    "inspect_input_path",
    "inspect_scientific_asset",
})
REFERENCE_PLAN_TOOL_NAMES = frozenset({
    "data_web_search",
    "data_web_download",
})
GENERATION_PLAN_TOOL_NAMES = frozenset({
    "execute_preprocessing_python",
    "execute_python",
    "generate_preprocessing_artifact",
    "build_scientific_preprocessing_package",
    "assemble_preprocessing_package",
    "prepare_scientific_mesh",
    "recover_atomic_structure",
})


def tool_allowed_in_plan_kind(tool_name: Any, plan_kind: str) -> bool:
    """Return whether a data tool belongs to the requested planning phase."""
    name = str(tool_name or "").strip()
    if plan_kind == "reference_evidence_only":
        return name in REFERENCE_PLAN_TOOL_NAMES
    if plan_kind == "preprocessing_generation":
        return name in GENERATION_PLAN_TOOL_NAMES
    return False


def canonical_workflow_capability(item: dict[str, Any] | str) -> str:
    """Derive one coarse executor capability from an AssetContract.

    The decision is based on normalized acquisition and representation fields,
    not on application names or arbitrary words in the explanation.  This is
    the single conversion layer between scientific operations and routing.
    """
    raw = item if isinstance(item, dict) else {"name_or_role": item}
    contract = normalize_asset_contract(raw)
    if contract.get("fulfillment_kind") in {"runtime_access", "runtime_tool"}:
        return "unclassified"
    capability = str(contract.get("workflow_capability") or "").strip().lower()
    return capability if capability in ROUTING_CAPABILITIES else "unclassified"


def normalize_task_scope_contract(
    scope: dict[str, Any] | None,
    required_files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize scientific operations into the closed routing contract.

    Unknown ``allowed_capabilities`` are retained as declared operations and
    cannot cause a schema failure.  Canonical routes are derived from the
    normalized asset contracts, so application-specific terminology does not
    require a growing alias table.
    """
    value = dict(scope) if isinstance(scope, dict) else {}
    raw_allowed = {
        str(item).strip().lower() for item in value.get("allowed_capabilities") or []
        if str(item).strip()
    }
    raw_excluded = {
        str(item).strip().lower() for item in value.get("excluded_capabilities") or []
        if str(item).strip()
    }
    declared = [
        str(item).strip() for item in (
            list(value.get("declared_operations") or [])
            + sorted(raw_allowed - ROUTING_CAPABILITIES)
            + sorted(raw_excluded - ROUTING_CAPABILITIES)
        ) if str(item).strip()
    ]
    # Preserve order while removing duplicate operation names.
    declared = list(dict.fromkeys(declared))
    derived = {
        canonical_workflow_capability(item)
        for item in (required_files or [])
        if isinstance(item, dict)
    }
    explicit_routes = raw_allowed & ROUTING_CAPABILITIES
    # The analyst's routing list is emitted alongside the AssetContracts. A
    # concrete required asset therefore completes an omitted route; free-form
    # wording alone never broadens the caller scope.
    derived_routes = derived & ROUTING_CAPABILITIES
    scope_origin = str(
        value.get("scope_origin") or value.get("source") or ""
    ).strip().lower()
    caller_scope_locked = bool(
        value.get("caller_scope_locked")
        or scope_origin in {"caller_request", "caller_contract", "upstream_contract"}
    )
    allowed = explicit_routes if caller_scope_locked else (explicit_routes or derived_routes)
    # A caller exclusion is authoritative. An Analyst-generated scope may be
    # reconciled with concrete required assets, including spatial assets.
    normalized_excluded = raw_excluded if caller_scope_locked else raw_excluded - derived_routes
    if not caller_scope_locked:
        allowed |= derived_routes
    if not allowed and not caller_scope_locked:
        allowed = {"local_artifact_generation"}
    # Capabilities stay asset-specific. A request for one generated file must
    # not silently authorize additional scripts or solver configurations.
    excluded = normalized_excluded & ROUTING_CAPABILITIES
    # Keep two distinct contract failures separate.  A route that is both
    # allowed and excluded is an internally inconsistent scope.  A route
    # required by an AssetContract but absent from an explicitly declared
    # scope is instead a missing authorization.  Treating both as one
    # ``contract_conflicts`` list made Analyst retries opaque and encouraged
    # fallback to silently lose required external inputs.
    allowed_excluded_conflicts = allowed & excluded
    required_capabilities_outside_scope = derived_routes - allowed
    result = dict(value)
    result["allowed_capabilities"] = sorted(allowed)
    result["excluded_capabilities"] = sorted(excluded)
    result["declared_operations"] = declared
    # ``contract_conflicts`` is the canonical persisted field for a true
    # allowed/excluded contradiction.
    result["contract_conflicts"] = sorted(allowed_excluded_conflicts)
    result["required_capabilities_outside_scope"] = sorted(required_capabilities_outside_scope)
    result["scope_origin"] = "caller_contract" if caller_scope_locked else (scope_origin or "analyst")
    result["caller_scope_locked"] = caller_scope_locked
    result.setdefault("discipline", "unknown")
    result.setdefault("workflow_kind", "scientific_preprocessing")
    result.setdefault("data_representations", [])
    result.setdefault("stage_ids", [])
    return result


def _normalize_asset_route_contract(item: dict[str, Any] | str) -> dict[str, Any]:
    """Canonicalize asset acquisition fields before any planning gate runs.

    The explicit source strategy is the routing authority.  Other acquisition
    fields are aliases supplied by an LLM and are rewritten to that strategy's
    canonical combination before validation.  This keeps contradictory draft
    aliases out of a plan without treating a repairable wording error as a
    planning failure.
    """
    value = item if isinstance(item, dict) else {}
    role = str(value.get("asset_role") or "").strip().lower()
    scientific_role = str(value.get("scientific_role") or "").strip().lower()
    role_tokens = _structured_role_tokens(
        scientific_role,
        role,
        value.get("name_or_role"),
    )
    # Credentials, account access and license entitlements are runtime access
    # prerequisites, not files the framework may discover or download.  This
    # decision is based exclusively on the structured scientific role; it
    # deliberately does not inspect a filename or a discipline/application.
    if scientific_role in RUNTIME_ACCESS_SCIENTIFIC_ROLES:
        return {
            "asset_role": "access_prerequisite",
            "scientific_role": scientific_role,
            "representation": str(value.get("representation") or "runtime_access").strip().lower() or "runtime_access",
            "workflow_capability": "unclassified",
            "source_strategy": "user_provided",
            "acquisition_kind": "runtime_access",
            "fulfillment_kind": "runtime_access",
            "is_external": False,
            "contradictory_fields": False,
        }
    # Software executables are execution-environment prerequisites, not data
    # assets.  The distinction uses the structured scientific role and
    # representation, so it applies across solvers without a filename list.
    declared_representation = str(value.get("representation") or "").strip().lower()
    if (
        declared_representation in {"runtime_tool", "executable"}
        or scientific_role in RUNTIME_TOOL_SCIENTIFIC_ROLES
        or bool(role_tokens & {"executable", "binary", "compiler", "interpreter"})
    ):
        return {
            "asset_role": "runtime_tool_requirement",
            "scientific_role": scientific_role,
            "representation": "runtime_tool",
            "workflow_capability": "unclassified",
            "source_strategy": "runtime_environment",
            "acquisition_kind": "runtime_tool",
            "fulfillment_kind": "runtime_tool",
            "is_external": False,
            "contradictory_fields": False,
        }
    if not scientific_role and role and role not in EXTERNAL_ASSET_ROLES:
        scientific_role = role
    strategy = str(value.get("source_strategy") or "").strip().lower()
    acquisition = str(value.get("acquisition_kind") or "").strip().lower()
    representation = str(value.get("representation") or "").strip().lower()
    declared_format = str(value.get("format") or value.get("type") or "").strip().lower()
    # A source/definition that is consumed by a mesher is not itself a mesh.
    # Normalize this at the shared AssetContract boundary so Analyst wording
    # cannot make a text producer compete with the registered mesh producer.
    if representation == "mesh" and role_tokens & {"definition", "script", "recipe", "source"}:
        representation = "text_artifact"
    structured_text = " ".join(
        str(value.get(key) or "")
        for key in ("scientific_role", "format", "type", "representation")
    )
    explicit_generated = bool(re.search(r"\b(?:generated|derived|local)\b|生成|派生|本地", structured_text, flags=re.I))
    # Strategy names a route; acquisition kind names the object acquired.
    # Prefer the former whenever it is explicit.  In particular, an Analyst
    # may describe a generated file as an ``external_dataset`` while copying
    # a template field from another row.  That stale alias must not turn a
    # local-generation request into an external-search plan (or make the
    # whole analysis unusable).
    # A manifest is the local description delivered by this node; external
    # datasets or files named inside it are source dependencies, not the
    # manifest itself.  An explicitly named upstream manifest remains an
    # external file contract.
    generated_manifest = (
        "manifest" in {declared_format, representation}
        and not str(value.get("expected_filename") or "").strip()
    )
    if generated_manifest:
        local = True
        strategy = "local_generation"
        acquisition = "generated_artifact"
    elif strategy in LOCAL_ASSET_STRATEGIES:
        local = True
    elif strategy in EXTERNAL_ASSET_STRATEGIES:
        local = False
    elif acquisition in LOCAL_ASSET_STRATEGIES:
        local = True
    elif acquisition in EXTERNAL_ACQUISITION_KINDS:
        local = False
    else:
        local = bool(explicit_generated and role not in EXTERNAL_ASSET_ROLES)
    # A mapping, lookup, descriptor, catalogue, or vocabulary is reference
    # material unless the plan supplies an explicit derivation recipe.  This
    # is an acquisition-semantic rule, not an application/file-name rule: a
    # text artifact writer cannot safely invent a domain mapping table merely
    # because a model labelled it "local_generation".
    reference_material = bool(role_tokens & {
        "mapping", "lookup", "table", "descriptor", "catalog", "catalogue",
        "ontology", "vocabulary",
    })
    has_generation_recipe = bool(
        value.get("generation_recipe") or value.get("generation_method")
        or value.get("derivation_method")
    )
    missing_generation_contract = bool(
        local and reference_material and not has_generation_recipe and not generated_manifest
    )
    acquisition_details = value.get("acquisition_contract")
    acquisition_details = acquisition_details if isinstance(acquisition_details, dict) else {}
    nested_retrieval = acquisition_details.get("retrieval_instructions")
    nested_retrieval = nested_retrieval if isinstance(nested_retrieval, dict) else {}
    acquisition_method = str(
        acquisition_details.get("method") or nested_retrieval.get("method") or ""
    ).strip().lower()
    official_reference_material = bool(
        reference_material
        and (
            role == "official_file_reference"
            or acquisition == "official_file_reference"
            or strategy == "official_repository"
            or acquisition_method in {"official_repository", "official_file", "repository_file"}
        )
    )
    if official_reference_material:
        role = "official_file_reference"
        strategy = "official_repository"
        acquisition = "official_file_reference"
    dataset = not official_reference_material and bool(
        re.search(r"\b(?:dataset|data set|netcdf|grib|zarr|hdf5|parquet|archive|reanalysis)\b|数据集|数据文件",
                  structured_text, flags=re.I)
        or representation in {
            "grib", "grib_dataset", "netcdf", "netcdf_dataset", "public_dataset", "dataset",
            # These are reusable scientific data models.  When their source
            # strategy is external they are dataset contracts, not an assumed
            # single repository file with a commit/hash requirement.
            "spatial_field", "time_series", "tensor", "spectrum", "particle_structure", "graph",
        }
        or acquisition in {"public_dataset", "external_dataset", "dataset"}
    )
    explicit_external = (
        role in EXTERNAL_ASSET_ROLES
        or strategy in EXTERNAL_ASSET_STRATEGIES
        or acquisition in EXTERNAL_ACQUISITION_KINDS
    )
    external = (
        explicit_external
        or dataset
    )
    if not representation:
        fmt = str(value.get("format") or value.get("type") or "").strip().lower()
        if "grib" in fmt:
            representation = "grib_dataset"
        elif "netcdf" in fmt or fmt in {"nc", ".nc"}:
            representation = "netcdf_dataset"
        elif dataset:
            representation = "dataset"
        elif re.search(r"mesh|geometry|cad", fmt, flags=re.I):
            representation = "geometry_or_mesh"
        elif re.search(r"python|shell|bash|json|namelist|config|script", fmt, flags=re.I):
            representation = "text_artifact"
    workflow_capability = str(value.get("workflow_capability") or "").strip().lower()
    contract_conflict = False
    if strategy in LOCAL_REUSE_STRATEGIES or (
        strategy not in EXTERNAL_ASSET_STRATEGIES and acquisition in LOCAL_REUSE_STRATEGIES
    ):
        return {
            "asset_role": "local_asset_reference",
            "scientific_role": scientific_role,
            "representation": representation or "unknown",
            "workflow_capability": "local_asset_reuse",
            "source_strategy": "local_reuse",
            "acquisition_kind": "local_asset",
            "is_external": False,
            "contradictory_fields": False,
        }
    # ``external_download`` describes transport, whereas
    # ``official_file_reference`` and ``external_dataset`` describe the
    # acquired object.  They are compatible aliases, not a conflicting pair.
    # The canonical branch below derives one stable contract from them.
    if local:
        expected_capability = (
            "mesh_generation"
            if representation == "mesh"
            else "preprocessing_script_generation"
            if re.search(r"python|shell|bash|script", structured_text, flags=re.I)
            else "configuration_generation"
            if re.search(r"namelist|config|configuration|json|yaml|toml|parameter|mapping|manifest", structured_text, flags=re.I)
            else "local_artifact_generation"
        )
        # Capability is a derived routing field for locally generated assets.
        # Accept a different local alias and normalize it here; only a
        # source/acquisition contradiction above remains an error.
        return {
            "asset_role": role if role and role not in EXTERNAL_ASSET_ROLES else "parameter_file",
            "scientific_role": scientific_role,
            "representation": representation or "text_artifact",
            "workflow_capability": expected_capability,
            "source_strategy": "local_generation",
            "acquisition_kind": "generated_artifact",
            "is_external": False,
            "contradictory_fields": missing_generation_contract,
            "missing_generation_contract": missing_generation_contract,
        }
    if external:
        if dataset:
            expected_capability = "dataset_acquisition"
            return {
                "asset_role": "external_dataset_reference",
                "scientific_role": scientific_role,
                "representation": representation or "dataset",
                "workflow_capability": "dataset_acquisition",
                "source_strategy": "external_download",
                "acquisition_kind": "external_dataset",
                "is_external": True,
                "contradictory_fields": False,
            }
        if representation in {"mesh"}:
            expected_capability = "mesh_generation"
            return {
                "asset_role": role or "official_file_reference",
                "scientific_role": scientific_role,
                "representation": representation,
                "workflow_capability": expected_capability,
                "source_strategy": "official_repository",
                "acquisition_kind": "official_file_reference",
                "is_external": True,
                "contradictory_fields": False,
            }
        if representation in {"structure", "atomic_structure", "crystal_structure"}:
            expected_capability = "atomic_structure_generation"
            return {
                "asset_role": role or "official_file_reference",
                "scientific_role": scientific_role,
                "representation": representation,
                "workflow_capability": expected_capability,
                "source_strategy": "official_repository",
                "acquisition_kind": "official_file_reference",
                "is_external": True,
                "contradictory_fields": False,
            }
        if representation in {"geometry", "geometry_or_mesh", "cad"}:
            expected_capability = "geometry_acquisition"
            return {
                "asset_role": role or "official_file_reference",
                "scientific_role": scientific_role,
                "representation": representation,
                "workflow_capability": "geometry_acquisition",
                "source_strategy": "official_repository",
                "acquisition_kind": "official_file_reference",
                "is_external": True,
                "contradictory_fields": False,
            }
        return {
            "asset_role": (
                role if role in {"external_dataset_reference", "restricted_asset_reference"}
                else ("external_dataset_reference" if dataset else (role or "official_file_reference"))
            ),
            "scientific_role": scientific_role,
            "representation": representation or "official_file",
            "workflow_capability": "official_file_acquisition",
            "source_strategy": "external_source" if dataset else "official_repository",
            "acquisition_kind": "external_dataset" if dataset else "official_file_reference",
            "is_external": True,
            "contradictory_fields": False,
        }
    representation_external = bool(re.search(
        r"\b(?:airfoil|aerofoil|coordinates?|geometry|mesh|surface|dataset|netcdf|grib|zarr|hdf5)\b|翼型|坐标|几何|网格|数据集",
        structured_text,
        flags=re.I,
    ))
    local_format = not representation_external and bool(re.search(
        r"\b(?:python(?:[_ -]?script)?|shell|bash|script|namelist|incar|kpoints|poscar|potcar|configuration|config|yaml|json|toml|template|dictionary|control|parameter[_ ]file|input[_ ]file|solver[_ ]input|preprocessing[_ ]artifact)\b",
        structured_text,
        flags=re.I,
    ))
    if local_format:
        return {
            "asset_role": role or "parameter_file",
            "scientific_role": scientific_role,
            "representation": representation or "text_artifact",
            "workflow_capability": "local_artifact_generation",
            "source_strategy": "local_generation",
            "acquisition_kind": "generated_artifact",
            "is_external": False,
            "contradictory_fields": False,
        }
    # Ambiguous prose is deliberately left unclassified.  It may be reviewed
    # as a generic requirement, but it cannot grant an external acquisition or
    # geometry route without an explicit acquisition contract.
    contract_conflict = bool(workflow_capability and workflow_capability != "unclassified")
    return {
        "asset_role": role or "unclassified_asset",
        "scientific_role": scientific_role,
        "representation": representation or "unknown",
        "workflow_capability": workflow_capability or "unclassified",
        "source_strategy": strategy or "unspecified",
        "acquisition_kind": acquisition or "unspecified",
        "is_external": False,
        "contradictory_fields": contract_conflict or missing_generation_contract,
        "missing_generation_contract": missing_generation_contract,
    }


def normalize_asset_contract(item: dict[str, Any] | str) -> dict[str, Any]:
    """Return the single routing *and delivery* contract for an asset.

    Acquisition and materialization are intentionally separate.  An external
    dataset is fulfilled in the data-node package by an actionable acquisition
    document, not by downloading its potentially very large payload.  Runtime
    outputs remain dependency contracts and are never fabricated as files.
    """
    value = item if isinstance(item, dict) else {"name_or_role": str(item or "")}
    contract = _normalize_asset_route_contract(value)
    fulfillment = str(
        value.get("fulfillment_kind") or contract.get("fulfillment_kind") or ""
    ).strip().lower()
    # An official repository file is an acquired input, never a runtime
    # product of its consumer stage.  Older analyses sometimes copied
    # ``runtime_output`` from the stage's expected-output list onto the same
    # asset.  Canonicalize that contradictory combination here so every
    # planner, validator, and reviewer sees the same delivery contract.
    official_file_contract = bool(
        contract.get("is_external")
        and contract.get("acquisition_kind") == "official_file_reference"
        and contract.get("workflow_capability") == "official_file_acquisition"
    )
    if official_file_contract and fulfillment in {
        "runtime_output", "runtime_access", "runtime_tool"
    }:
        consumers = item.get("consumer_stages")
        if isinstance(consumers, str):
            consumers = [consumers]
        fulfillment = "stage_input" if item.get("stage_id") or consumers else "acquisition_document"
    if fulfillment in {"runtime_output", "runtime_access", "runtime_tool"}:
        materialization = fulfillment
    elif contract.get("source_strategy") == "local_reuse":
        materialization = "local_reuse"
    elif contract.get("is_external"):
        materialization = "acquisition_document"
    else:
        materialization = "generated_file"

    raw_identity = (
        value.get("logical_asset_id")
        or (value.get("tool_identity") if materialization == "runtime_tool" else None)
        or value.get("id")
        or value.get("name_or_role")
        or value.get("scientific_role")
        or value.get("asset_role")
        or "asset"
    )
    logical_asset_id = re.sub(
        r"[^a-z0-9]+", "_", str(raw_identity).strip().casefold()
    ).strip("_") or "asset"
    consumers = value.get("consumer_stages")
    if not isinstance(consumers, list):
        consumers = [consumers] if consumers not in (None, "") else []
    owner_stage = str(value.get("stage_id") or value.get("owner_stage") or "").strip()
    if owner_stage and owner_stage not in consumers:
        consumers.append(owner_stage)

    return {
        **contract,
        "logical_asset_id": logical_asset_id,
        "materialization_kind": materialization,
        "owner_stage": owner_stage or None,
        "producer_stage": str(value.get("producer_stage") or "").strip() or None,
        "consumer_stages": [
            str(stage).strip() for stage in consumers if str(stage).strip()
        ],
        "delivery_required": (
            False
            if materialization in {"runtime_output", "runtime_access", "runtime_tool"}
            else True
            if official_file_contract
            else bool(value.get("delivery_required", True))
        ),
        # External datasets carry a retrieval contract rather than their
        # payload. Exact official files are the exception: a consuming stage
        # needs the verified file itself in the package.
        "requires_local_payload": (
            official_file_contract
            or materialization not in {
                "acquisition_document", "runtime_output", "runtime_access", "runtime_tool",
            }
        ),
    }


def _validate_output_paths_contract(
    step_id: str,
    output_paths: Any,
    outputs: set[str],
) -> list[str]:
    errors: list[str] = []
    if not isinstance(output_paths, dict):
        return [f"generation step {step_id} needs tool_arguments.output_paths"]
    declared = {str(key) for key in output_paths}
    if declared != outputs:
        return [
            f"generation step {step_id} output_paths keys must exactly match outputs: "
            f"expected {sorted(outputs)}, got {sorted(declared)}"
        ]
    for output_id, raw_path in output_paths.items():
        path = PurePosixPath(str(raw_path or ""))
        if not str(raw_path or "").strip() or path.is_absolute() or ".." in path.parts:
            errors.append(
                f"generation step {step_id} output path for {output_id!r} must be a relative workspace path"
            )
    return errors


def _validate_python_generation_contract(
    step_id: str,
    arguments: dict[str, Any],
    outputs: set[str],
) -> list[str]:
    """Validate the generic executable-file contract before any subprocess runs."""
    errors: list[str] = []
    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        return [f"generation step {step_id} execute_preprocessing_python needs non-empty Python source in tool_arguments.code"]
    try:
        tree = ast.parse(code, filename=f"{step_id}.py", mode="exec")
    except SyntaxError as exc:
        return [
            f"generation step {step_id} execute_preprocessing_python code is not valid Python: "
            f"{exc.msg} (line {exc.lineno})"
        ]
    errors.extend(_validate_output_paths_contract(step_id, arguments.get("output_paths"), outputs))
    if errors:
        return errors
    # Runtime execution verifies that declared files are created and non-empty.
    # Do not require a complete path to appear as one source literal: valid
    # writers commonly compose it from an output directory or constants.
    return errors


def _step_workflow_capability(step: dict[str, Any]) -> str:
    """Resolve the executable capability used by both schema and executor gates."""
    arguments = step.get("tool_arguments") if isinstance(step.get("tool_arguments"), dict) else {}
    tool_name = str(step.get("tool_name") or "").strip().lower()
    explicit = str(
        arguments.get("workflow_capability") or step.get("workflow_capability") or ""
    ).strip().lower()
    asset_kind = str(arguments.get("asset_kind") or "").strip().lower()
    if tool_name in REFERENCE_PLAN_TOOL_NAMES:
        # A documentation-only reference request supports a local
        # configuration/script contract; it is not an exact external-file
        # acquisition.  Preserve that explicit capability for the plan and
        # executor gates instead of classifying every web search as a file
        # download.
        if asset_kind == "reference" and explicit in {
            "configuration_generation",
            "preprocessing_script_generation",
        }:
            return explicit
        return {
            "dataset": "dataset_acquisition",
            "public_dataset": "dataset_acquisition",
            "external_dataset": "dataset_acquisition",
            "geometry": "geometry_acquisition",
            "geometry_or_mesh": "geometry_acquisition",
            "mesh": "mesh_generation",
            "structure": "atomic_structure_generation",
        }.get(asset_kind, "official_file_acquisition")
    if tool_name == "generate_preprocessing_artifact":
        # This writer intentionally supports several local artifact classes.
        # The output AssetContract selects the route; a generic ``text``
        # format cannot override that structured decision.
        if explicit in {
            "local_artifact_generation",
            "configuration_generation",
            "preprocessing_script_generation",
        }:
            return explicit
        spec = arguments.get("artifact_spec") if isinstance(arguments.get("artifact_spec"), dict) else {}
        fmt = str(spec.get("format") or arguments.get("format") or "").lower()
        if re.search(r"python|shell|bash|script", fmt):
            return "preprocessing_script_generation"
        if re.search(r"namelist|config|configuration|json|yaml|toml|manifest|parameter", fmt):
            return "configuration_generation"
        return "local_artifact_generation"
    if tool_name in {"execute_preprocessing_python", "execute_python"}:
        return "local_artifact_generation"
    if tool_name == "prepare_scientific_mesh":
        return "mesh_generation"
    if tool_name in {"recover_atomic_structure"}:
        return "atomic_structure_generation"
    if tool_name in {"build_scientific_preprocessing_package", "assemble_preprocessing_package"}:
        # Package assembly is executor infrastructure. It stages already
        # authorized assets and does not create a new scientific configuration.
        return "local_artifact_generation"
    return "unclassified"


def is_pipeline_infrastructure_step(step: dict[str, Any]) -> bool:
    """Return whether a step stages an already-authorized delivery."""
    return str(step.get("tool_name") or "").strip() in {
        "build_scientific_preprocessing_package",
        "assemble_preprocessing_package",
    }


def _step_capability_contract_error(step: dict[str, Any]) -> str:
    """Reject a model-declared route that disagrees with the tool contract."""
    arguments = step.get("tool_arguments") if isinstance(step.get("tool_arguments"), dict) else {}
    explicit = str(arguments.get("workflow_capability") or step.get("workflow_capability") or "").strip().lower()
    if not explicit:
        return ""
    derived = _step_workflow_capability(step)
    if derived == "unclassified":
        return f"tool {step.get('tool_name') or '?'} has no canonical capability"
    if explicit == derived:
        return ""
    return f"workflow_capability {explicit!r} disagrees with tool-derived capability {derived!r}"


def _merge_consumer_acquisition_parameters(plan: dict[str, Any]) -> None:
    """Complete acquisition requests from their declared consumer bindings.

    The acquisition document and the consuming script are two views of one
    stage contract.  Designer drafts often preserve the dataset locator but
    omit a binding such as ``pressure_level`` or ``variable``.  Treating that
    mechanical omission as a new planning problem caused identical Designer
    retries.  Merge only unambiguous values from the existing interface; a
    conflicting value remains a validation error.
    """
    deliverables = [
        item for item in _list(plan.get("required_deliverables"))
        if isinstance(item, dict)
    ]
    by_id = {
        str(item.get("id") or ""): item
        for item in deliverables
        if str(item.get("id") or "").strip()
    }
    candidates: dict[str, dict[str, list[Any]]] = {}
    for step in _list(plan.get("generation_steps")):
        if not isinstance(step, dict):
            continue
        arguments = step.get("tool_arguments") if isinstance(step.get("tool_arguments"), dict) else {}
        spec = arguments.get("artifact_spec") if isinstance(arguments.get("artifact_spec"), dict) else {}
        interface = spec.get("stage_interface_contract") if isinstance(spec.get("stage_interface_contract"), dict) else {}
        bindings = spec.get("parameter_bindings")
        if not isinstance(bindings, dict) or not bindings:
            outputs = [str(value) for value in _list(step.get("outputs")) if str(value)]
            if len(outputs) == 1 and isinstance(by_id.get(outputs[0]), dict):
                bindings = by_id[outputs[0]].get("parameter_bindings")
        if not isinstance(bindings, dict) or not bindings:
            continue
        for external in _list(interface.get("external_inputs")):
            if not isinstance(external, dict):
                continue
            external_id = str(external.get("logical_asset_id") or "").strip()
            if not external_id or external_id not in by_id:
                continue
            per_key = candidates.setdefault(external_id, {})
            for key, value in bindings.items():
                if value in (None, "", [], {}):
                    continue
                values = per_key.setdefault(str(key), [])
                if not any(value == existing for existing in values):
                    values.append(deepcopy(value))

    for external_id, per_key in candidates.items():
        item = by_id[external_id]
        if normalize_asset_contract(item).get("acquisition_kind") != "external_dataset":
            continue
        contract = normalize_acquisition_contract(item)
        retrieval = dict(contract.get("retrieval_instructions") or {})
        request = dict(retrieval.get("request_parameters") or {})
        changed = False
        for key, values in per_key.items():
            if key not in request and len(values) == 1:
                request[key] = deepcopy(values[0])
                changed = True
        if not changed:
            continue
        retrieval["request_parameters"] = request
        contract["retrieval_instructions"] = retrieval
        item["acquisition_contract"] = contract
        for step in _list(plan.get("generation_steps")):
            if not isinstance(step, dict) or external_id not in {
                str(value) for value in _list(step.get("outputs"))
            }:
                continue
            arguments = step.get("tool_arguments") if isinstance(step.get("tool_arguments"), dict) else {}
            spec = arguments.get("artifact_spec") if isinstance(arguments.get("artifact_spec"), dict) else {}
            if spec:
                spec["acquisition_contract"] = deepcopy(contract)


def _normalize_registered_tool_arguments(step: dict[str, Any]) -> None:
    """Normalize lossless transport shapes from the registered tool contract.

    Models differ in whether they encode a structured specification as a JSON
    object or as JSON text.  The tool registry is the authority for that wire
    shape, so normalize it once before both validation and execution rather
    than teaching every planner/model combination its own exception.
    """
    arguments = step.get("tool_arguments")
    definition = get_tool(str(step.get("tool_name") or ""))
    if not isinstance(arguments, dict) or definition is None:
        return
    schema = definition.parameters_schema if isinstance(definition.parameters_schema, dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for name, value in list(arguments.items()):
        field = properties.get(name) if isinstance(properties.get(name), dict) else {}
        if field.get("type") == "string" and isinstance(value, (dict, list)):
            arguments[name] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _registered_tool_argument_errors(step: dict[str, Any]) -> list[str]:
    """Validate top-level planned arguments against their single registry schema."""
    definition = get_tool(str(step.get("tool_name") or ""))
    arguments = step.get("tool_arguments")
    if definition is None or not isinstance(arguments, dict):
        return []
    schema = definition.parameters_schema if isinstance(definition.parameters_schema, dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    errors: list[str] = []
    for name in schema.get("required") or []:
        if arguments.get(name) in (None, ""):
            errors.append(f"tool argument {name} is required")
    expected_types = {
        "string": str,
        "object": dict,
        "array": list,
        "boolean": bool,
        "integer": int,
        "number": (int, float),
    }
    for name, value in arguments.items():
        field = properties.get(name) if isinstance(properties.get(name), dict) else {}
        declared = field.get("type")
        allowed = declared if isinstance(declared, list) else [declared]
        python_types = tuple(
            value_type for item in allowed
            if (value_type := expected_types.get(item)) is not None
        )
        if python_types and not isinstance(value, python_types):
            errors.append(f"tool argument {name} must be {' or '.join(map(str, allowed))}")
    return errors


def normalize_plan(raw: dict[str, Any]) -> dict[str, Any]:
    plan = deepcopy(raw) if isinstance(raw, dict) else {}
    plan["schema_version"] = str(plan.get("schema_version") or SCHEMA_VERSION)
    explicit_kind = str(plan.get("plan_kind") or "").strip()
    if explicit_kind not in {"reference_evidence_only", "preprocessing_generation"}:
        steps = _list(plan.get("generation_steps"))
        only_search = bool(steps) and all(
            isinstance(step, dict)
            and tool_allowed_in_plan_kind(step.get("tool_name"), "reference_evidence_only")
            for step in steps
        )
        plan["plan_kind"] = "reference_evidence_only" if only_search else "preprocessing_generation"
    else:
        plan["plan_kind"] = explicit_kind
    plan["task_summary"] = str(plan.get("task_summary") or "").strip()
    plan["discipline"] = _dict(plan.get("discipline"))
    plan["simulation_software"] = _dict(plan.get("simulation_software"))
    software = plan["simulation_software"]
    if not str(software.get("name") or "").strip():
        for alias in ("primary", "application", "software", "solver", "model"):
            if str(software.get(alias) or "").strip():
                software["name"] = software[alias]
                break
    plan["requirement_analysis"] = _dict(plan.get("requirement_analysis"))
    plan["preprocessing_request"] = deepcopy(
        plan.get("preprocessing_request")
        if isinstance(plan.get("preprocessing_request"), dict)
        else _dict(plan["requirement_analysis"].get("preprocessing_request"))
    )
    plan["preprocessing_work_order"] = deepcopy(
        plan.get("preprocessing_work_order")
        if isinstance(plan.get("preprocessing_work_order"), dict)
        else _dict(plan["requirement_analysis"].get("preprocessing_work_order"))
    )
    plan["review_profile"] = str(
        plan.get("review_profile")
        or plan["preprocessing_request"].get("review_profile")
        or plan["requirement_analysis"].get("review_profile")
        or "plan_bound"
    ).strip()
    plan["task_scope"] = deepcopy(
        plan["requirement_analysis"].get("task_scope")
        if isinstance(plan["requirement_analysis"].get("task_scope"), dict)
        else _dict(plan.get("task_scope"))
    )
    plan["required_deliverables"] = _list(plan.get("required_deliverables"))
    analysis_requirements = {
        str(item.get("id") or "").casefold(): item
        for item in _list(plan["requirement_analysis"].get("required_files"))
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    # Every deliverable carries the same canonical asset contract used by the
    # Analyst, Designer repair, Critic, and executor gates.
    for item in plan["required_deliverables"]:
        if not isinstance(item, dict):
            continue
        requirement = analysis_requirements.get(str(item.get("id") or "").casefold()) or {}
        # Analyst-selected names are execution proposals, not caller authority.
        path_keys = ("declared_output_path", "output_path", "relative_path",
                     "expected_filename", "filename", "file_name", "path")
        paths = [str(value) for source in (requirement, item) for key in path_keys
                 if (value := source.get(key))]
        request = plan["preprocessing_request"]
        if plan["review_profile"] == "request_bound" and request:
            authority = caller_request_text(request, preprocessing_request=request)
            authority += "\n" + json.dumps(request.get("requested_assets") or [], ensure_ascii=False)
            item["output_path_is_explicit"] = any(
                re.search(r"(?<![A-Za-z0-9_./-])" + re.escape(path) + r"(?![A-Za-z0-9_./-])", authority)
                for path in paths
            )
        else:
            item["output_path_is_explicit"] = bool(paths) if requirement else item.get("output_path_is_explicit", True)
        for key in (
            "name", "name_or_role", "reason", "requirement_basis", "format",
            "scientific_role", "representation", "source_strategy", "acquisition_kind",
            "workflow_capability", "evidence", "acceptance_criteria", "resume_contract",
            "access_method", "dataset_identifier", "provider", "url", "endpoint",
            "request_template", "acquisition_contract",
            "delivery_mode", "size_estimate", "estimated_size", "estimated_size_bytes", "size_bytes",
            "source_discovery", "download_workflow", "reference_status", "reference_evidence",
            "stage_id", "declared_output_path", "parameter_bindings",
            "parameter_bindings_required", "parameter_binding_basis", "parameter_binding_source",
            "parameter_binding_mode", "parameter_binding_assertions",
        ):
            if key == "parameter_bindings" and isinstance(item.get(key), dict):
                continue
            if item.get(key) in (None, "", [], {}) and requirement.get(key) not in (None, "", [], {}):
                item[key] = deepcopy(requirement[key])
        contract = normalize_asset_contract(item)
        item.update({key: value for key, value in contract.items() if key != "contradictory_fields"})
        item["workflow_capability"] = canonical_workflow_capability(item)
        if contract.get("acquisition_kind") == "external_dataset":
            # RequirementAnalysis is the authoritative source for an
            # external route.  A Designer may omit or partially rewrite the
            # nested contract; retaining that incomplete copy makes a valid
            # Analyst locator look like a missing retrieval contract and
            # needlessly sends the same draft through another Designer round.
            draft_contract = normalize_acquisition_contract({
                **item,
                "acquisition_contract": item.get("acquisition_contract") or {},
            })
            authoritative_contract = normalize_acquisition_contract({
                **requirement,
                "acquisition_contract": requirement.get("acquisition_contract") or {},
            }) if requirement else {}
            if (
                authoritative_contract
                and not acquisition_contract_errors(authoritative_contract)
            ):
                item["acquisition_contract"] = authoritative_contract
            else:
                item["acquisition_contract"] = draft_contract
        if isinstance(item.get("parameter_bindings"), dict) and item["parameter_bindings"]:
            item["parameter_bindings_required"] = True
    plan["task_scope"] = normalize_task_scope_contract(
        plan["task_scope"],
        [item for item in plan["required_deliverables"] if isinstance(item, dict)],
    )
    plan["generation_steps"] = _list(plan.get("generation_steps"))
    deliverables_by_id = {
        str(item.get("id") or ""): item
        for item in plan["required_deliverables"]
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    stage_contracts_by_id = {
        str(item.get("stage_id") or ""): item
        for item in _list(plan["requirement_analysis"].get("stage_input_contracts"))
        if isinstance(item, dict) and str(item.get("stage_id") or "")
    }
    script_deliverable_count_by_stage: dict[str, int] = {}
    for deliverable in plan["required_deliverables"]:
        if not isinstance(deliverable, dict):
            continue
        stage_id = str(deliverable.get("stage_id") or "").strip()
        if (
            stage_id
            and canonical_workflow_capability(deliverable) == "preprocessing_script_generation"
        ):
            script_deliverable_count_by_stage[stage_id] = (
                script_deliverable_count_by_stage.get(stage_id, 0) + 1
            )
    for step in plan["generation_steps"]:
        if not isinstance(step, dict):
            continue
        _normalize_registered_tool_arguments(step)
        if plan["review_profile"] == "request_bound" and not str(step.get("work_unit_id") or "").strip():
            first_output = next((str(item) for item in _list(step.get("outputs")) if str(item)), "")
            if first_output:
                step["work_unit_id"] = f"wu_{first_output}"
        if str(step.get("tool_name") or "") == "generate_preprocessing_artifact":
            arguments = step.get("tool_arguments") if isinstance(step.get("tool_arguments"), dict) else {}
            spec = arguments.get("artifact_spec") if isinstance(arguments.get("artifact_spec"), dict) else {}
            if spec:
                arguments["artifact_spec"] = normalize_artifact_spec(spec)
                spec = arguments["artifact_spec"]
            outputs = [str(item) for item in _list(step.get("outputs")) if str(item)]
            deliverable = deliverables_by_id.get(outputs[0]) if len(outputs) == 1 else None
            if (
                isinstance(deliverable, dict)
                and not spec.get("parameter_bindings")
            ):
                bindings = deliverable.get("parameter_bindings")
                if isinstance(bindings, dict) and bindings:
                    spec["parameter_bindings"] = deepcopy(bindings)
            if isinstance(deliverable, dict):
                for key in (
                    "parameter_binding_mode",
                    "parameter_binding_source",
                    "parameter_binding_assertions",
                ):
                    if spec.get(key) in (None, "", [], {}) and deliverable.get(key) not in (None, "", [], {}):
                        spec[key] = deepcopy(deliverable[key])
            if spec.get("parameter_bindings") and not spec.get("parameter_binding_mode"):
                # Executable scripts commonly translate symbolic research
                # values into API/solver encodings.  Configuration text keeps
                # literal checking unless the plan explicitly declares a
                # different representation.
                capability = _step_workflow_capability(step)
                if capability == "preprocessing_script_generation":
                    spec["parameter_binding_mode"] = "semantic"
                else:
                    spec["parameter_binding_mode"] = "literal"
            if (
                isinstance(deliverable, dict)
                and normalize_asset_contract(deliverable).get("acquisition_kind")
                == "external_dataset"
            ):
                spec.setdefault("artifact_kind", "external_dataset_acquisition")
                spec.setdefault("acquisition_context", {
                    key: deepcopy(deliverable.get(key))
                    for key in (
                        "name", "name_or_role", "representation", "scientific_role",
                        "requirement_basis", "evidence", "acceptance_criteria", "stage_id",
                        "provider", "access_method", "dataset_identifier", "url", "endpoint",
                        "request_template", "request_parameters", "retrieval_instructions",
                        "delivery_mode", "size_estimate", "estimated_size", "estimated_size_bytes",
                        "size_bytes", "source_discovery", "download_workflow", "reference_status",
                        "reference_evidence",
                    )
                    if deliverable.get(key) not in (None, "", [], {})
                })
                # Acquisition summaries are a framework-owned serialization
                # contract.  Designer supplies the route and source metadata;
                # it never has to reproduce the final JSON document.
                spec["acquisition_contract"] = normalize_acquisition_contract({
                    **deliverable,
                    **spec.get("acquisition_context", {}),
                    "acquisition_contract": spec.get("acquisition_contract") or {},
                })
            stage_id = str(step.get("stage_id") or "")
            stage_interface = stage_contracts_by_id.get(stage_id)
            if stage_interface:
                # The framework, not Designer prose, owns cross-stage paths.
                # The artifact writer receives this immutable logical
                # producer/consumer contract so independently generated
                # scripts cannot invent incompatible input/output locations.
                interface = deepcopy(stage_interface)
                interface.pop("stage_parameters", None)
                required_paths = [
                    str(item.get("canonical_path") or "").strip()
                    for key in ("external_inputs", "upstream_runtime_inputs")
                    for item in interface.get(key) or []
                    if isinstance(item, dict) and str(item.get("canonical_path") or "").strip()
                ]
                # Only a stage invocation/driver owns all declared runtime
                # outputs.  A stage may also contain independent helper
                # scripts; forcing every helper to mention every stage output
                # creates false review failures and incompatible repair loops.
                scientific_role = str(
                    (deliverable or {}).get("scientific_role")
                    or (deliverable or {}).get("name_or_role")
                    or ""
                ).strip().casefold()
                owns_runtime_outputs = (
                    scientific_role in {
                        "stage_execution_script", "stage_invocation", "execution_driver",
                    }
                    or (
                        isinstance(deliverable, dict)
                        and canonical_workflow_capability(deliverable)
                        == "preprocessing_script_generation"
                        and script_deliverable_count_by_stage.get(stage_id) == 1
                    )
                )
                if owns_runtime_outputs:
                    required_paths.extend(
                        str(item.get("canonical_path") or "").strip()
                        for item in interface.get("runtime_outputs") or []
                        if isinstance(item, dict) and str(item.get("canonical_path") or "").strip()
                    )
                interface["required_paths"] = list(dict.fromkeys(required_paths))
                spec["stage_interface_contract"] = interface
        capability = _step_workflow_capability(step)
        if capability != "unclassified":
            arguments = step.setdefault("tool_arguments", {})
            if isinstance(arguments, dict):
                arguments.setdefault("workflow_capability", capability)
    _merge_consumer_acquisition_parameters(plan)
    plan["tool_requirements"] = [
        {
            "capability": str(item.get("capability") or "").strip(),
            "selected_tools": [str(value) for value in _list(item.get("selected_tools"))],
        }
        for item in _list(plan.get("tool_requirements"))
        if isinstance(item, dict)
    ]
    plan["assumptions"] = _list(plan.get("assumptions"))
    plan["unresolved_questions"] = _list(plan.get("unresolved_questions"))
    plan["risks"] = _list(plan.get("risks"))
    plan["reproducibility"] = _dict(plan.get("reproducibility"))
    return plan


def validate_plan(raw: dict[str, Any]) -> dict[str, Any]:
    plan = normalize_plan(raw)
    errors: list[str] = []
    warnings: list[str] = []
    review_profile = str(plan.get("review_profile") or "plan_bound")
    if plan.get("preprocessing_request"):
        errors.extend(validate_preprocessing_request(plan["preprocessing_request"]))
    if plan.get("preprocessing_work_order"):
        errors.extend(validate_preprocessing_work_order(plan["preprocessing_work_order"]))
    if review_profile not in {"plan_bound", "request_bound"}:
        errors.append("review_profile must be plan_bound or request_bound")
    if not plan["task_summary"]:
        errors.append("task_summary is required")
    if review_profile == "plan_bound" and not str(plan["discipline"].get("primary") or "").strip():
        errors.append("discipline.primary is required")
    if review_profile == "plan_bound" and not str(plan["simulation_software"].get("name") or "").strip():
        errors.append("simulation_software.name is required")
    analysis = plan["requirement_analysis"]
    if review_profile == "plan_bound" and not str(analysis.get("calculation_type") or "").strip():
        errors.append("requirement_analysis.calculation_type is required")
    if not _list(analysis.get("evidence")):
        errors.append("requirement_analysis.evidence is required")
    stages = analysis.get("calculation_stages") or []
    if stages and not isinstance(stages, list):
        errors.append("requirement_analysis.calculation_stages must be an array")
        stages = []
    stage_ids: set[str] = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            errors.append(f"calculation_stages[{index}] must be an object")
            continue
        stage_id = str(stage.get("id") or "").strip()
        if not stage_id:
            errors.append(f"calculation_stages[{index}].id is required")
        elif stage_id in stage_ids:
            errors.append(f"duplicate calculation stage id: {stage_id}")
        else:
            stage_ids.add(stage_id)
        if not str(stage.get("calculation_type") or "").strip():
            errors.append(f"calculation stage {stage_id or index} needs calculation_type")
        if not isinstance(stage.get("parameters") or {}, dict):
            errors.append(f"calculation stage {stage_id or index} parameters must be an object")
        if not _list(stage.get("evidence")):
            errors.append(f"calculation stage {stage_id or index} needs evidence")
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        unknown = set(str(item) for item in _list(stage.get("dependencies"))) - stage_ids
        if unknown:
            errors.append(
                f"calculation stage {stage.get('id') or '?'} has unknown dependencies: {sorted(unknown)}"
            )
    calculation_type_text = str(analysis.get("calculation_type") or "")
    if (
        not stages
        and any(token in calculation_type_text.lower() for token in ("multiple", "multi-stage", "workflow"))
    ):
        errors.append(
            "requirement_analysis declares multiple calculation stages but calculation_stages is empty"
        )
    scope = plan.get("task_scope") if isinstance(plan.get("task_scope"), dict) else {}
    if plan.get("generation_steps") and not scope:
        errors.append("task_scope is required for executable plans")
    for field in ("discipline", "workflow_kind", "declared_operations", "allowed_capabilities", "excluded_capabilities", "data_representations", "stage_ids"):
        if scope and field not in scope:
            errors.append(f"task_scope.{field} is required")
    allowed_capabilities = {
        str(item).strip().lower() for item in scope.get("allowed_capabilities") or []
    }
    excluded_capabilities = {
        str(item).strip().lower() for item in scope.get("excluded_capabilities") or []
    }
    supported_capabilities = set(ROUTING_CAPABILITIES)
    unknown_scope_capabilities = (allowed_capabilities | excluded_capabilities) - supported_capabilities
    if unknown_scope_capabilities:
        errors.append(
            f"task_scope contains unsupported capabilities: {sorted(unknown_scope_capabilities)}"
        )
    if scope.get("contract_conflicts"):
        errors.append(
            f"task_scope has contradictory allowed/excluded capabilities: "
            f"{sorted(str(item) for item in scope.get('contract_conflicts') or [])}"
        )
    if plan.get("generation_steps") and not allowed_capabilities:
        errors.append("task_scope.allowed_capabilities must not be empty for executable plans")
    for step in plan.get("generation_steps") or []:
        if not isinstance(step, dict):
            continue
        # Reference discovery/acquisition has its own one-request plan and
        # ledger.  A generation plan may consume only the evidence/artifact
        # produced by that route; otherwise a Designer can silently invent a
        # URL after a search result and bypass provenance verification.
        plan_kind = str(plan.get("plan_kind") or "preprocessing_generation")
        if not tool_allowed_in_plan_kind(step.get("tool_name"), plan_kind):
            errors.append(
                f"generation step {step.get('id') or '?'} tool {step.get('tool_name') or '?'} "
                f"is not allowed in {plan_kind}"
            )
        capability = _step_workflow_capability(step)
        infrastructure_step = is_pipeline_infrastructure_step(step)
        capability_error = _step_capability_contract_error(step)
        if capability_error:
            errors.append(f"generation step {step.get('id') or '?'} {capability_error}")
        if capability == "unclassified":
            errors.append(f"generation step {step.get('id') or '?'} needs workflow_capability")
        elif not infrastructure_step and (
            capability not in allowed_capabilities or capability in excluded_capabilities
        ):
            errors.append(
                f"generation step {step.get('id') or '?'} capability {capability} is outside task scope"
            )

    deliverable_ids: set[str] = set()
    required_ids: set[str] = set()
    for index, item in enumerate(plan["required_deliverables"]):
        if not isinstance(item, dict):
            errors.append(f"required_deliverables[{index}] must be an object")
            continue
        # Contracts are normalized before this validation pass.  Only an
        # unresolved contract can be contradictory here; stale draft aliases
        # have already been replaced with the canonical route.
        normalized_asset = normalize_asset_contract(item)
        if normalized_asset.get("missing_generation_contract"):
            errors.append(
                f"required_deliverables[{index}] local reference material needs an explicit generation_recipe"
            )
        elif normalized_asset.get("contradictory_fields"):
            errors.append(f"required_deliverables[{index}] has contradictory asset acquisition fields")
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            errors.append(f"required_deliverables[{index}].id is required")
        elif item_id in deliverable_ids:
            errors.append(f"duplicate deliverable id: {item_id}")
        else:
            deliverable_ids.add(item_id)
        # A verified local asset is materialized directly by the publisher,
        # not by an LLM-selected generation tool.  It remains a required
        # package member, but must not be forced through a fictitious writer
        # step merely to satisfy the generation-DAG schema.
        reference_status = str(item.get("reference_status") or "").strip().lower()
        if (
            item.get("required", True)
            and str(item.get("source_strategy") or "") != "local_reuse"
            and reference_status not in {"resolved", "downloaded_verified"}
            and item_id
        ):
            required_ids.add(item_id)
        if not _list(item.get("acceptance_criteria")):
            errors.append(f"deliverable {item_id or index} needs acceptance_criteria")
        if not str(item.get("requirement_basis") or "").strip():
            errors.append(f"deliverable {item_id or index} needs requirement_basis")
        if not _list(item.get("evidence")):
            errors.append(f"deliverable {item_id or index} needs evidence")
        # Installation and executable paths are runtime dependencies, not
        # Research Plan values. Reuse the writer's placeholder detector at
        # the plan boundary so an approved plan cannot fail only after a
        # generated file has already been written.
        bindings = item.get("parameter_bindings")
        if isinstance(bindings, dict) and bindings:
            markers = blocked_placeholder_markers(
                json.dumps(bindings, ensure_ascii=False, default=str)
            )
            if markers:
                errors.append(
                    f"deliverable {item_id or index} parameter_bindings contain unresolved placeholders: "
                    + ", ".join(markers)
                )
    if not deliverable_ids:
        errors.append("at least one required_deliverable is required")

    step_ids: set[str] = set()
    generated_outputs: set[str] = set()
    producer_steps: dict[str, dict[str, Any]] = {}
    dependencies: dict[str, list[str]] = {}
    analysis = plan.get("requirement_analysis") if isinstance(plan.get("requirement_analysis"), dict) else {}
    mesh_dependent_required_ids = {
        str(item.get("id") or "").strip()
        for item in _list(analysis.get("required_files"))
        if isinstance(item, dict)
        and item.get("requires_mesh_generation") is True
        and str(item.get("id") or "").strip()
    }
    declared_stage_ids = {
        str(stage.get("id") or "").strip()
        for stage in _list(analysis.get("calculation_stages"))
        if isinstance(stage, dict) and str(stage.get("id") or "").strip()
    }
    script_deliverable_count_by_stage: dict[str, int] = {}
    configuration_deliverable_count_by_stage: dict[str, int] = {}
    external_consumers: dict[str, list[tuple[str, dict[str, Any], bool]]] = {}
    for deliverable in plan["required_deliverables"]:
        if not isinstance(deliverable, dict):
            continue
        owner = str(deliverable.get("stage_id") or "").strip()
        if owner and canonical_workflow_capability(deliverable) == "preprocessing_script_generation":
            script_deliverable_count_by_stage[owner] = script_deliverable_count_by_stage.get(owner, 0) + 1
        if owner and canonical_workflow_capability(deliverable) == "configuration_generation":
            configuration_deliverable_count_by_stage[owner] = configuration_deliverable_count_by_stage.get(owner, 0) + 1
    for index, item in enumerate(plan["generation_steps"]):
        if not isinstance(item, dict):
            errors.append(f"generation_steps[{index}] must be an object")
            continue
        step_id = str(item.get("id") or "").strip()
        if not step_id:
            errors.append(f"generation_steps[{index}].id is required")
            continue
        if step_id in step_ids:
            errors.append(f"duplicate generation step id: {step_id}")
        step_ids.add(step_id)
        stage_id = str(item.get("stage_id") or "").strip()
        if plan["plan_kind"] == "preprocessing_generation" and review_profile == "plan_bound" and not stage_id:
            errors.append(f"generation step {step_id} needs explicit stage_id")
        if plan["plan_kind"] == "preprocessing_generation" and review_profile == "request_bound" and not str(item.get("work_unit_id") or "").strip():
            errors.append(f"generation step {step_id} needs work_unit_id for request_bound execution")
        if plan["plan_kind"] == "preprocessing_generation" and review_profile == "plan_bound" and stage_id == "package":
            if str(item.get("tool_name") or "") != "build_scientific_preprocessing_package":
                errors.append(
                    f"generation step {step_id} may use stage_id=package only for an explicit package step"
                )
        elif plan["plan_kind"] == "preprocessing_generation" and review_profile == "plan_bound" and stage_id and stage_id not in declared_stage_ids:
            errors.append(
                f"generation step {step_id} stage_id {stage_id!r} is not declared by requirement_analysis.calculation_stages"
            )
        dependencies[step_id] = [str(v) for v in _list(item.get("dependencies"))]
        if not str(item.get("action") or "").strip():
            errors.append(f"generation step {step_id} needs action")
        if not str(item.get("tool_capability") or "").strip():
            errors.append(f"generation step {step_id} needs tool_capability")
        if not str(item.get("tool_name") or "").strip():
            errors.append(f"generation step {step_id} needs tool_name")
        if not isinstance(item.get("tool_arguments"), dict):
            errors.append(f"generation step {step_id} needs tool_arguments object")
        else:
            errors.extend(
                f"generation step {step_id} {error}"
                for error in _registered_tool_argument_errors(item)
            )
        outputs = {str(v) for v in _list(item.get("outputs")) if str(v).strip()}
        generated_outputs.update(outputs)
        for output_id in outputs:
            producer_steps.setdefault(output_id, item)
        for output_id in outputs:
            deliverable = next(
                (entry for entry in plan["required_deliverables"]
                 if isinstance(entry, dict) and str(entry.get("id") or "") == output_id),
                None,
            )
            deliverable_stage = str((deliverable or {}).get("stage_id") or "").strip()
            if plan["plan_kind"] == "preprocessing_generation" and review_profile == "plan_bound" and deliverable_stage and deliverable_stage != stage_id:
                errors.append(
                    f"generation step {step_id} stage_id must match deliverable {output_id} stage_id"
                )
        unknown_outputs = outputs - deliverable_ids
        if unknown_outputs:
            errors.append(f"generation step {step_id} has unknown outputs: {sorted(unknown_outputs)}")
        output_deliverable = next(
            (
                entry for entry in plan["required_deliverables"]
                if isinstance(entry, dict) and str(entry.get("id") or "") in outputs
            ),
            None,
        ) if len(outputs) == 1 else None
        if str(item.get("tool_name") or "").strip() == "execute_preprocessing_python":
            errors.extend(_validate_python_generation_contract(
                step_id,
                item.get("tool_arguments") if isinstance(item.get("tool_arguments"), dict) else {},
                outputs,
            ))
        if str(item.get("tool_name") or "").strip() == "generate_preprocessing_artifact":
            arguments = item.get("tool_arguments") if isinstance(item.get("tool_arguments"), dict) else {}
            mesh_dependent_outputs = outputs & mesh_dependent_required_ids
            if mesh_dependent_outputs:
                errors.append(
                    f"generation step {step_id} cannot use the generic text writer for "
                    f"mesh-dependent deliverables: {sorted(mesh_dependent_outputs)}"
                )
            if len(outputs) != 1:
                errors.append(
                    f"generation step {step_id} generate_preprocessing_artifact must produce exactly one deliverable"
                )
            if not isinstance(arguments.get("artifact_spec"), dict) or not arguments["artifact_spec"]:
                errors.append(f"generation step {step_id} generate_preprocessing_artifact needs artifact_spec")
            else:
                spec = arguments["artifact_spec"]
                interface = spec.get("stage_interface_contract")
                if isinstance(interface, dict):
                    bindings_for_consumer = spec.get("parameter_bindings")
                    if not isinstance(bindings_for_consumer, dict) or not bindings_for_consumer:
                        bindings_for_consumer = (
                            output_deliverable.get("parameter_bindings")
                            if isinstance(output_deliverable, dict) else {}
                        )
                    if isinstance(bindings_for_consumer, dict) and bindings_for_consumer:
                        mode = str(spec.get("parameter_binding_mode") or "").casefold()
                        for external in interface.get("external_inputs") or []:
                            if not isinstance(external, dict):
                                continue
                            external_id = str(external.get("logical_asset_id") or "").strip()
                            if external_id:
                                external_consumers.setdefault(external_id, []).append(
                                    (step_id, bindings_for_consumer, mode == "semantic")
                                )
                required_spec_fields = {
                    "purpose", "parameter_sources", "format", "evidence", "acceptance_criteria",
                }
                missing_spec_fields = sorted(
                    field for field in required_spec_fields
                    if spec.get(field) in (None, "", [], {})
                )
                if missing_spec_fields:
                    errors.append(
                        f"generation step {step_id} artifact_spec missing fields: {missing_spec_fields}"
                    )
                if not (
                    isinstance(spec.get("parameter_sources"), list)
                    and all(isinstance(value, str) for value in spec["parameter_sources"])
                ):
                    errors.append(f"generation step {step_id} artifact_spec.parameter_sources must be a string array")
                if not (
                    isinstance(spec.get("evidence"), list)
                    and all(isinstance(value, dict) for value in spec["evidence"])
                ):
                    errors.append(f"generation step {step_id} artifact_spec.evidence must be an object array")
                if not (
                    isinstance(spec.get("acceptance_criteria"), list)
                    and all(isinstance(value, str) for value in spec["acceptance_criteria"])
                ):
                    errors.append(f"generation step {step_id} artifact_spec.acceptance_criteria must be a string array")
                # The Designer selects a fulfilment route and describes the
                # required artifact; it must not smuggle final file content
                # into the plan.  The approved executor performs generation,
                # acquisition, and format validation in its own stage.
                forbidden_spec_fields = sorted(
                    field for field in ("content", "code", "script_content", "file_contents")
                    if field in spec
                )
                if forbidden_spec_fields:
                    errors.append(
                        f"generation step {step_id} artifact_spec must not contain final file content: {forbidden_spec_fields}"
                    )
                if str(spec.get("artifact_kind") or "") == "external_dataset_acquisition":
                    contract_errors = acquisition_contract_errors(
                        spec.get("acquisition_contract") or spec
                    )
                    if contract_errors:
                        errors.append(
                            f"generation step {step_id} acquisition_contract missing fields: {contract_errors}"
                        )
                # A configuration with explicit Research Plan values needs a
                # file-level semantic contract before execution.  Previously
                # the Reviewer treated a missing binding map as success,
                # making it impossible to distinguish an accurate solver
                # input from a syntactically valid but unrelated file.
                bindings = spec.get("parameter_bindings")
                if not isinstance(bindings, dict) or not bindings:
                    bindings = (
                        output_deliverable.get("parameter_bindings")
                        if isinstance(output_deliverable, dict) else None
                    )
                binding_mode = str(spec.get("parameter_binding_mode") or "").strip().casefold()
                if binding_mode and binding_mode not in {"literal", "semantic"}:
                    errors.append(
                        f"generation step {step_id} artifact_spec.parameter_binding_mode must be literal or semantic"
                    )
                assertions = spec.get("parameter_binding_assertions")
                if assertions not in (None, "", [], {}) and not (
                    isinstance(assertions, list) and all(isinstance(item, dict) for item in assertions)
                ):
                    errors.append(
                        f"generation step {step_id} artifact_spec.parameter_binding_assertions must be an object array"
                    )
                capability_requires_binding = (
                    _step_workflow_capability(item) == "configuration_generation"
                    or (
                        _step_workflow_capability(item) == "preprocessing_script_generation"
                        and script_deliverable_count_by_stage.get(stage_id) == 1
                        and configuration_deliverable_count_by_stage.get(stage_id, 0) == 0
                    )
                )
                if (
                    capability_requires_binding
                    and str(spec.get("artifact_kind") or "") != "external_dataset_acquisition"
                    and bool(
                        spec.get("parameter_bindings_required")
                        or (
                            isinstance(output_deliverable, dict)
                            and output_deliverable.get("parameter_bindings_required")
                        )
                    )
                    and (not isinstance(bindings, dict) or not bindings)
                ):
                    errors.append(
                        f"generation step {step_id} artifact requires parameter_bindings "
                        f"for explicit stage values"
                    )
            forbidden_argument_fields = sorted(
                field for field in ("content", "code", "script_content", "file_contents")
                if field in arguments
            )
            if forbidden_argument_fields:
                errors.append(
                    f"generation step {step_id} must not contain final file content: {forbidden_argument_fields}"
                )
            errors.extend(_validate_output_paths_contract(step_id, arguments.get("output_paths"), outputs))
        if not _list(item.get("verification")):
            errors.append(f"generation step {step_id} needs verification")
    if not step_ids:
        errors.append("at least one generation_step is required")
    deliverables_by_id = {
        str(item.get("id") or ""): item
        for item in plan["required_deliverables"]
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    for external_id, consumers in external_consumers.items():
        external = deliverables_by_id.get(external_id) or {}
        if normalize_asset_contract(external).get("acquisition_kind") != "external_dataset":
            continue
        contract = external.get("acquisition_contract") or external
        for consumer_step_id, bindings, semantic in consumers:
            consistency_errors = acquisition_contract_consistency_errors(
                contract,
                bindings,
                semantic=semantic,
            )
            errors.extend(
                f"acquisition contract {external_id} incompatible with consumer {consumer_step_id}: {error}"
                for error in consistency_errors
            )
    missing_outputs = required_ids - generated_outputs
    if missing_outputs:
        errors.append(f"required deliverables lack generation steps: {sorted(missing_outputs)}")
    if plan.get("plan_kind") == "preprocessing_generation":
        package_present = any(
            is_pipeline_infrastructure_step(step)
            for step in plan.get("generation_steps") or []
            if isinstance(step, dict)
        )
        package_required = any(
            item_id in required_ids
            and str((producer_steps.get(item_id) or {}).get("tool_name") or "")
            != "generate_preprocessing_artifact"
            for item_id in deliverable_ids
        )
        if package_required and not package_present:
            errors.append(
                "generated scientific assets require a package assembly step for review and dataset publication"
            )
    if plan.get("plan_kind") == "preprocessing_generation" and review_profile == "request_bound":
        requested_ids = {
            str(asset_id).strip()
            for unit in (plan.get("preprocessing_work_order") or {}).get("work_units") or []
            if isinstance(unit, dict)
            for asset_id in unit.get("requested_asset_ids") or []
            if str(asset_id).strip()
        }
        missing_requested = requested_ids - deliverable_ids
        if missing_requested:
            errors.append(
                "request-bound work order assets missing from required_deliverables: "
                f"{sorted(missing_requested)}"
            )
    # Dedicated scientific routes are checked from normalized asset contracts,
    # never by rescanning caller prose. Reference plans have their own tool set.
    if plan.get("plan_kind") == "preprocessing_generation":
        for deliverable in plan["required_deliverables"]:
            if not isinstance(deliverable, dict):
                continue
            if canonical_workflow_capability(deliverable) != "mesh_generation":
                continue
            deliverable_id = str(deliverable.get("id") or "").strip()
            if not deliverable_id:
                continue
            producer = producer_steps.get(deliverable_id) or {}
            producer_capability = _step_workflow_capability(producer) if producer else ""
            if producer_capability and producer_capability != "mesh_generation":
                errors.append(
                    f"mesh deliverable {deliverable_id!r} must be produced by "
                    f"prepare_scientific_mesh, but its producing step uses "
                    f"{producer.get('tool_name') or 'unknown tool'} "
                    f"(capability={producer_capability})"
                )
    # The data node prepares inputs for the complete caller-supplied DAG,
    # not just the stages that happened to receive a Designer deliverable.
    # RequirementAnalysis supplies the authoritative stage-owned input
    # contracts; require the plan to retain each one by its semantic role or
    # declared output path.  Runtime products and runtime access are excluded
    # because they are produced/provided only when the downstream stage runs.
    if plan.get("plan_kind") == "preprocessing_generation":
        def stage_asset_key(value: Any) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

        stage_input_requirements = [
            item for item in _list(analysis.get("required_files"))
            if isinstance(item, dict)
            and str(item.get("stage_id") or "").strip()
            and item.get("delivery_required") is not False
            and str(item.get("fulfillment_kind") or "") not in {"runtime_output", "runtime_access", "runtime_tool"}
        ]
        delivered_aliases_by_stage: dict[str, set[str]] = {}
        for item in plan["required_deliverables"]:
            if not isinstance(item, dict):
                continue
            stage_id = str(item.get("stage_id") or "").strip()
            if not stage_id:
                continue
            aliases = {
                stage_asset_key(item.get(field))
                for field in ("id", "name", "type", "declared_output_path", "filename", "file_name")
                if stage_asset_key(item.get(field))
            }
            delivered_aliases_by_stage.setdefault(stage_id, set()).update(aliases)
        uncovered_stage_inputs: list[str] = []
        for requirement in stage_input_requirements:
            stage_id = str(requirement.get("stage_id") or "").strip()
            required_aliases = {
                stage_asset_key(requirement.get(field))
                for field in ("id", "name_or_role")
                if stage_asset_key(requirement.get(field))
            }
            if not required_aliases & delivered_aliases_by_stage.get(stage_id, set()):
                uncovered_stage_inputs.append(
                    f"{stage_id}:{requirement.get('name_or_role') or requirement.get('id')}"
                )
        if uncovered_stage_inputs:
            errors.append(
                "stage input contracts missing from required_deliverables: "
                + ", ".join(uncovered_stage_inputs)
            )
    for step_id, deps in dependencies.items():
        unknown = set(deps) - step_ids
        if unknown:
            errors.append(f"generation step {step_id} has unknown dependencies: {sorted(unknown)}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> bool:
        if step_id in visiting:
            return True
        if step_id in visited:
            return False
        visiting.add(step_id)
        for dep in dependencies.get(step_id, []):
            if dep in dependencies and visit(dep):
                return True
        visiting.remove(step_id)
        visited.add(step_id)
        return False

    if any(visit(step_id) for step_id in dependencies):
        errors.append("generation_steps dependency graph contains a cycle")

    authorized_tools: set[str] = set()
    for index, item in enumerate(plan["tool_requirements"]):
        if not isinstance(item, dict):
            errors.append(f"tool_requirements[{index}] must be an object")
            continue
        if not str(item.get("capability") or "").strip():
            errors.append(f"tool_requirements[{index}].capability is required")
        selected_tools = [str(value) for value in _list(item.get("selected_tools"))]
        authorized_tools.update(selected_tools)
        for tool_name in selected_tools:
            definition = get_tool(tool_name)
            if definition is None:
                errors.append(f"selected tool {tool_name!r} is not registered")
            elif definition.allowed_node_types is not None and "data" not in definition.allowed_node_types:
                errors.append(f"selected tool {tool_name!r} is not allowed for data")
    for item in plan["generation_steps"]:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        if tool_name and tool_name not in authorized_tools:
            errors.append(
                f"generation step {item.get('id') or '?'} uses tool {tool_name!r} "
                "that is not selected by tool_requirements"
            )

    blocking_questions = [
        q for q in plan["unresolved_questions"]
        if isinstance(q, dict) and q.get("blocking", True)
    ]
    if blocking_questions:
        errors.append(f"{len(blocking_questions)} blocking unresolved question(s) remain")
    repro = plan["reproducibility"]
    if not repro.get("workspace_layout"):
        errors.append("reproducibility.workspace_layout is required")
    if not _list(repro.get("provenance_records")):
        errors.append("reproducibility.provenance_records is required")
    return {"valid": not errors, "errors": errors, "warnings": warnings, "plan": plan}


def normalize_critique(
    raw: dict[str, Any],
    validation: dict[str, Any],
    overall_score_min: float = 8.0,
) -> dict[str, Any]:
    critique = deepcopy(raw) if isinstance(raw, dict) else {}
    scores_in = _dict(critique.get("dimension_scores"))
    scores: dict[str, float] = {}
    for name in CRITIC_DIMENSIONS:
        try:
            score = float(scores_in.get(name, 0))
        except (TypeError, ValueError):
            score = 0.0
        scores[name] = round(max(0.0, min(10.0, score)), 2)
    weighted = sum(scores[name] * CRITIC_WEIGHTS[name] for name in CRITIC_DIMENSIONS)
    critical = [str(v) for v in _list(critique.get("critical_concerns")) if str(v).strip()]
    if not validation.get("valid"):
        critical.extend(f"Schema gate: {item}" for item in validation.get("errors") or [])
    major = [str(v) for v in _list(critique.get("major_concerns")) if str(v).strip()]
    changes = [str(v) for v in _list(critique.get("recommended_changes")) if str(v).strip()]
    min_score = min(scores.values()) if scores else 0.0
    approved = weighted >= overall_score_min and min_score >= 7.0 and not critical and validation.get("valid", False)
    return {
        "overall_score": round(weighted, 2),
        "dimension_scores": scores,
        "critical_concerns": critical,
        "major_concerns": major,
        "recommended_changes": changes,
        "decision": "approve" if approved else "revise",
        "approval_requirements": {
            "overall_score_min": overall_score_min,
            "dimension_score_min": 7.0,
            "critical_concerns_allowed": 0,
            "schema_must_be_valid": True,
        },
    }
