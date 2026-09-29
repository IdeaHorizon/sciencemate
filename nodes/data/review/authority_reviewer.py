"""Model-assisted review against the caller's locked preprocessing authority."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from core.llm import LLMMessage
from core.state import State
from nodes.data.pipeline_contract import revision_contract
from nodes.data.planning.request_contract import caller_request_text


_SYSTEM_PROMPT = """You review scientific preprocessing deliveries for the data node.
Your primary question is whether the delivered assets satisfy the original locked user, experiment,
or research-plan request without omissions or scope expansion. Your secondary question is whether
their quality is supported by the supplied native-tool validation and reference evidence. Do not add
requirements or preferred defaults that the authority did not request. Native validators own only the
format, topology, or solver-compatibility facts that their supplied receipts explicitly report. A
deterministic pass is not evidence of semantic request compliance when no native semantic check exists.
Keep evidence dimensions and scope distinct: surface measures cannot establish volume measures,
and aggregate counts cannot establish per-element validity. Request the missing evidence instead.
Inspect the materialized content for every remaining condition; do not treat content omitted from model
context as an empty/placeholder file. Distinguish a requested physical property from the representation
used to store or execute it. Interpret format-specific fields through supplied parser and native-validator
evidence; do not invent semantics for a field name or require a representation the authority did not name.
Treat listed names as required members, not an exclusive set, unless the locked authority explicitly
says otherwise. For alternatives ("or"/"或"), use the work order's selected representation rather
than requiring every alternative. Additional members are forbidden only when the authority explicitly
says exactly/only/no additional members. A suggested/example numeric value remains advisory even when
it accompanies a mandatory qualitative goal; judge that goal from materialized or native-validator
evidence. Treat the value itself as mandatory only when the authority explicitly locks that value or
places it in acceptance criteria.
For every explicit value, geometry, format, or boundary name in the authority, verify materialized
generator configuration or native evidence. State the requested and observed values in the evidence
observation, including units and reference points; a successful native check cannot explain away a
different requested value. Check these independently of delivery-list failures. Comments, headings, README text, requested text, and a
filename are declarations of intent, not proof that geometry, connectivity, loads, or boundaries were
materialized. A pass must provide short exact non-comment excerpts from the delivered files and explain
what each excerpt proves. When parser-derived mesh metrics are supplied, use their connectivity, boundary
loop, area, and coordinate facts to test requested geometry instead of trusting descriptive mesh comments.
Never invent or paraphrase an excerpt. Short verbatim lines may omit intervening lines;
keep their original order and separate omissions with an ellipsis. Avoid copying large blocks.
Equivalent numeric spellings represent the same value; an explicitly written matching value is not
missing because of its formatting. Resolve conflicting observations before issuing repair instructions.
When a distributed quantity is materialized as discrete nodal, cell, or sample values, assess its
weights and integrated value against the requested density or intensity. A different keyword or a
per-entry value is not evidence of a mismatch by itself. If equivalence cannot be established from the
available materialized evidence, request evidence instead of prescribing a preferred representation.
List every independently testable original-request condition in condition_checks, including qualitative
or spatial requirements, and link each to zero-based indices in evidence. A material/format excerpt
cannot also prove geometry, region membership or a spatial distribution. Do not claim the entire request
passed from a few compliant fields. Use needs_evidence for an unverified condition before requesting
regeneration. Producer evidence is internal source/configuration, not an extra required deliverable:
cross-check it against observed output metrics. Separate object definition, assignment, and activation:
a named group proves neither a constraint/load applied to it nor that the active configuration uses it.
For an operational condition, trace the target to its actual assignment and applicable execution scope;
cite those records, not just the target's existence. For spatial refinement, compare measured local sizes
or density with the surrounding region; names and global element counts alone cannot prove it.
Additional refinement is not automatically a defect unless it contradicts the requested distribution.
Do not invent numerical thresholds or require a minimum element count or convergence study that the
caller did not request. Distinguish preprocessing compliance from unverified solution accuracy.
When a defect originates upstream, cite its source_file from producer_evidence alongside the affected
delivery file, so the original source can be repaired and downstream outputs rebuilt.
For each explicit numerical constraint, include a comparison in its evidence: requested, observed,
operator (eq/ge/le), unit, and scope. Convert both values to the same units and reference frame first.
Preserve quantifiers: "at least" is ge, "at most" is le, and "every/all" applies to each relevant
member or the worst-case bound, not a convenient maximum or average. One compliant direction,
region or member cannot compensate for another that violates the requirement. Use actual measured
or materialized values, not planned parameters. Omit comparisons for advisory values or quantities
that cannot be reliably derived; explain the evidence limitation or request the missing evidence.
The reviewed files are staged before atomic publication. A request for a separate output directory
is satisfied by the supplied request-scoped delivery_destination; do not require a duplicate nested
directory inside that destination.

Submit the result through `submit_authority_review` when that function is available; otherwise return
one compact JSON object with:
- status: pass, fail, or needs_evidence
- requested_files: inventory paths to inspect when status is needs_evidence
- asset_bindings: objects with asset_id and paths, linking required deliverables to materialized inventory files
- summary: concise conclusion
- evidence: objects with file, exact excerpt, and observation for the materialized facts that support the conclusion
- condition_checks: objects with condition (one explicit caller condition) and evidence_indices
- issues: only actionable mismatches, each containing code, severity (critical or major), file,
  message, and recommendation

A failure must cite a specific unmet authority requirement and the evidence supporting that finding.
If a necessary file's content was omitted or sampled, request it with needs_evidence instead of
reporting a missing or defective asset. README can prove instructions were delivered, but cannot
prove that a physical property was materialized.
Resolve every required deliverable to inventory files in asset_bindings, using their actual content.
The original request is authoritative; Analyst-selected methods and their proposed intermediate files
are not additional caller requirements. delivery_plan is execution context, not frozen authority:
do not require an inferred script, filename, or report syntax just because it is listed there.
Conversely, delivery_required=false cannot waive any condition in the original request.
When the caller permits alternative methods, evaluate the
method actually used. Do not demand configuration for an unused method. Bind an equivalent generated
definition or embedded validation evidence if it fulfills the original purpose; explain the equivalence
in evidence. This does not waive any explicit requested file, scientific value, or acceptance condition.
When output_path_is_explicit is false, declared_output_path is only a proposed name: an equivalent
materialized file at another path can fulfill the asset. Do not demand a duplicate copy or rename.
An explicit filename without directories may resolve to a unique inventory filename; an explicit
directory path must match that location. Never bind unrelated files to hide a missing deliverable.
An unfulfilled identity in deterministic_review is a request to check this mapping, not proof that
generation failed. Report genuinely missing content as an actionable issue with its deliverable_id.
Do not expose chain-of-thought; provide conclusions and evidence only."""

_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_authority_review",
        "description": "Submit the final semantic review of the staged preprocessing delivery.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pass", "fail", "needs_evidence"]},
                "requested_files": {"type": "array", "items": {"type": "string"}},
                "asset_bindings": {
                    "type": "array", "items": {
                        "type": "object", "properties": {
                            "asset_id": {"type": "string"},
                            "paths": {"type": "array", "items": {"type": "string"}},
                        }, "required": ["asset_id", "paths"],
                    },
                },
                "summary": {"type": "string"},
                "condition_checks": {
                    "type": "array", "items": {
                        "type": "object", "properties": {
                            "condition": {"type": "string"},
                            "evidence_indices": {"type": "array", "items": {"type": "integer"}},
                        }, "required": ["condition", "evidence_indices"],
                    },
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "excerpt": {"type": "string"},
                            "observation": {"type": "string"},
                            "comparison": {
                                "type": "object", "properties": {
                                    "requested": {"type": "number"}, "observed": {"type": "number"},
                                    "operator": {"type": "string", "enum": ["eq", "ge", "le"]},
                                    "unit": {"type": "string"}, "scope": {"type": "string"},
                                }, "required": ["requested", "observed", "operator", "unit", "scope"],
                            },
                        },
                        "required": ["file", "excerpt", "observation"],
                    },
                },
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "severity": {"type": "string", "enum": ["critical", "major"]},
                            "file": {"type": "string"},
                            "message": {"type": "string"},
                            "deliverable_id": {"type": "string"},
                            "source_file": {"type": "string"},
                            "recommendation": {"type": "string"},
                        },
                        "required": ["code", "severity", "file", "message", "recommendation"],
                    },
                },
            },
            "required": ["status", "summary", "evidence", "issues", "asset_bindings", "condition_checks"],
        },
    },
}


def _requirement_list(
    request: dict[str, Any],
    work_order: dict[str, Any],
    plan_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Keep original authority intact; the model links its conditions to evidence.

    Conditions are derived from this request, not a domain-specific checklist.
    File integrity and logical asset identities remain deterministic gates.
    """
    snapshot = request.get("authority_snapshot") if isinstance(request.get("authority_snapshot"), dict) else {}
    inputs = request.get("inputs")
    caller_input = next(
        (item for item in inputs if isinstance(item, dict)),
        inputs if isinstance(inputs, dict) else None,
    ) if isinstance(inputs, list) else inputs
    # The append-only authority snapshot is the review source of truth.
    # ``inputs`` may contain an orchestrator's implementation-oriented
    # paraphrase and therefore cannot add constraints to the caller's words.
    original_request = caller_request_text(snapshot) or caller_request_text(caller_input)
    analysis = (
        plan_context.get("requirement_analysis")
        if isinstance(plan_context.get("requirement_analysis"), dict)
        else {}
    )
    user_authority = str((request.get("authority") or {}).get("kind") or "") == "user_request"
    deliverables = [
        item for item in plan_context.get("required_deliverables") or []
        if isinstance(item, dict) and item.get("delivery_required", True) is not False
    ]
    plan_bound = request.get("review_profile") == "plan_bound"
    return [{
        "requirement_id": "authority:locked_request",
        "requirement": {
            "original_request": original_request,
            "purpose": request.get("purpose"),
            "requested_assets": (
                snapshot.get("requested_assets") or []
                if user_authority else request.get("requested_assets") or []
            ),
            "acceptance_criteria": (
                snapshot.get("acceptance_criteria") or []
                if user_authority else request.get("acceptance_criteria") or []
            ),
            "non_goals": (
                snapshot.get("non_goals") or []
                if user_authority else request.get("non_goals") or []
            ),
            "work_units": (work_order.get("work_units") or []) if plan_bound else [],
            "required_deliverables": deliverables if plan_bound else [],
            "delivery_destination": plan_context.get("delivery_destination"),
            "preprocessing_stages": (
                analysis.get("calculation_stages") or []
                if request.get("review_profile") == "plan_bound" else []
            ),
            "instruction": (
                "Verify every explicit caller condition against the delivered files. "
                "Do not replace the original request with generic file-quality checks."
            ),
        },
    }]


def _file_evidence(
    files: dict[str, Any],
    *,
    package_dir: str | Path | None = None,
    total_limit: int = 60000,
) -> dict[str, str]:
    evidence: dict[str, str] = {}
    remaining = total_limit
    candidates = {name: value.get("content", "") if isinstance(value, dict) else value
                  for name, value in files.items()}
    sizes: dict[str, int] = {
        str(name): int(value["size_bytes"]) if isinstance(value, dict) and "size_bytes" in value
        else len(str(candidates[name] or "").encode("utf-8", errors="replace"))
        for name, value in files.items()
    }
    root = Path(package_dir).resolve() if package_dir else None
    if root and root.is_dir():
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            name = str(path.relative_to(root))
            sizes[name] = path.stat().st_size
            if name in candidates:
                continue
            size = path.stat().st_size
            readable = (
                size <= 12000
                and (
                    path.suffix.lower() in {".json", ".yaml", ".yml", ".md", ".txt", ".log", ".geo", ".toml", ".ini"}
                    or path.name in {"boundary", "controlDict", "fvSchemes", "fvSolution", "INCAR", "KPOINTS", "POSCAR"}
                )
            )
            content = (
                path.read_text(encoding="utf-8", errors="replace")
                if readable else ""
            )
            candidates[name] = content

    # Read small files fully before sampling large ones. Reserve a fair share
    # for every readable file, irrespective of discipline or directory name.
    ordered = sorted(candidates.items(), key=lambda item: (len(item[1]), item[0]))
    unread = sum(bool(content) for _, content in ordered)
    for name, content in ordered:
        text = str(content or "")
        size = sizes.get(str(name), len(text.encode("utf-8", errors="replace")))
        if remaining <= 0 or not text:
            evidence[str(name)] = (
                "[materialized file; content omitted from model context; "
                f"size={size} bytes; integrity and parsing are owned by deterministic review]"
            )
            continue
        limit = remaining // max(unread, 1)
        unread -= 1
        if len(text) > limit:
            # A head/tail-only excerpt hides the middle of solver decks where
            # connectivity and parameter blocks commonly live.  Uniform
            # sampling is format-neutral and gives the reviewer evidence from
            # the complete materialized asset without loading huge meshes.
            width = max(0, (limit - 192) // 4)
            span = max(len(text) - width, 0)
            starts = [round(index * span / 3) for index in range(4)]
            excerpt = "\n[... materialized content omitted ...]\n".join(
                text[start:start + width] for start in starts
            )
            evidence[str(name)] = (
                f"[materialized file excerpt; size={size} bytes]\n{excerpt}"
            )
        else:
            excerpt = text
            sampled = isinstance(files.get(name), dict) and files[name].get("sampled")
            evidence[str(name)] = f"[materialized file excerpt; size={size} bytes]\n{text}" if sampled else text
        remaining -= len(excerpt)
    return evidence


def _review_request_context(request: dict[str, Any]) -> dict[str, Any]:
    """Keep the authority payload focused on the immutable caller contract.

    ``authority_snapshot`` can contain the entire upstream transcript/KB.  It
    is useful for provenance, but it is not another review requirement and
    sending it to the reviewer competes with the delivery evidence.  The
    original caller text is already represented by ``authority_requirements``.
    """
    fields = (
        "schema_version", "request_id", "request_spec_hash", "request_kind",
        "review_profile", "authority", "purpose", "consumer",
        "requested_assets", "acceptance_criteria", "non_goals",
        "scientific_authority", "source_artifact_ids",
    )
    compact = {
        key: request.get(key)
        for key in fields
        if request.get(key) not in (None, "", [], {})
    }
    return compact


def _extract_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        parsed = None
        for index, char in enumerate(value):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(value[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
    if not isinstance(parsed, dict):
        raise ValueError("authority reviewer returned no JSON object")
    return parsed


def _reviewer_error(code: str, message: str) -> dict[str, Any]:
    issues = [{
        "code": code,
        "severity": "critical",
        "file": "audit/preprocessing_review.json",
        "message": message,
        "recommendation": "Retry only the authority review without changing generated assets or the approved work order.",
        "repairable": False,
    }]
    return {
        "review_type": "model_authority_alignment",
        "status": "error",
        "failure_category": "review_protocol_error",
        "summary": message,
        "requirement_checks": [],
        "issues": issues,
    }


def _verdict_check(
    parsed: dict[str, Any],
    requirement_id: str,
    files: dict[str, str],
) -> dict[str, Any]:
    """Map a model verdict to the framework-owned review receipt."""
    raw_status = str(
        parsed.get("status")
        or parsed.get("verdict")
        or parsed.get("decision")
        or parsed.get("review_status")
        or ""
    ).strip().lower()
    status = {
        "pass": "pass", "passed": "pass", "approve": "pass", "approved": "pass",
        "success": "pass", "satisfied": "pass",
        "fail": "fail", "failed": "fail", "reject": "fail", "rejected": "fail",
        "revise": "fail", "needs_revision": "fail", "unsatisfied": "fail",
    }.get(raw_status)
    issues = [item for item in parsed.get("issues") or [] if isinstance(item, dict)]
    reason = str(
        parsed.get("summary")
        or parsed.get("reason")
        or parsed.get("conclusion")
        or parsed.get("message")
        or ""
    ).strip()
    if not reason and issues:
        reason = "; ".join(
            str(item.get("message") or item.get("code") or "").strip()
            for item in issues
            if str(item.get("message") or item.get("code") or "").strip()
        )
    if status not in {"pass", "fail"} or not reason or not files:
        raise ValueError("Review requires pass/fail, a summary, and readable materialized evidence.")
    evidence: list[dict[str, Any]] = []
    evidence_indices: dict[int, int] = {}
    evidence_errors: dict[int, str] = {}
    for index, item in enumerate(parsed.get("evidence") or []):
        if not isinstance(item, dict):
            continue
        file_name = str(item.get("file") or "").strip()
        excerpt = str(item.get("excerpt") or "").strip()
        observation = str(item.get("observation") or "").strip()
        content = str(files.get(file_name) or "")
        # A citation may select several verbatim lines, not necessarily one
        # contiguous block. JSON-embedded tool logs carry escaped newlines.
        comment_pattern = r"^(?:#|//|!|;|%|\*\*|<!--)"
        compact_content = " ".join(" ".join(
            line for line in content.replace("\\n", "\n").replace('\\"', '"').splitlines()
            if not re.match(comment_pattern, line.strip())
        ).split())
        active_lines = [line.strip() for line in excerpt.replace("\\n", "\n").replace('\\"', '"').splitlines()
                        if line.strip() and line.strip() not in {"...", "…", "[...]"}
                        and not re.match(comment_pattern, line.strip())]
        cursor = 0
        matched = bool(active_lines)
        for line in active_lines:
            fragment = " ".join(line.split())
            position = compact_content.find(fragment, cursor)
            if position < 0:
                matched = False
                evidence_errors[index] = f"{file_name}: text not found in order: {fragment[:200]}"
                break
            cursor = position + len(fragment)
        if file_name not in files or not active_lines or not observation:
            evidence_errors[index] = f"{file_name}: use an available inventory path, non-comment excerpt and observation"
        if (
            file_name in files
            and bool(observation)
            and len(excerpt) >= 3
            and matched
        ):
            evidence_indices[index] = len(evidence)
            evidence.append({
                "file": file_name,
                "excerpt": excerpt,
                "observation": observation,
            })
            comparison = item.get("comparison")
            if isinstance(comparison, dict):
                expected, actual = comparison.get("requested"), comparison.get("observed")
                operator = comparison.get("operator")
                if (not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                            and math.isfinite(value) for value in (expected, actual))
                        or operator not in {"eq", "ge", "le"}):
                    raise ValueError(f"evidence[{index}].comparison requires finite numbers and operator eq/ge/le.")
                evidence[-1]["comparison"] = comparison
                satisfied = math.isclose(actual, expected, rel_tol=1e-9, abs_tol=0.0) or (
                    actual > expected if operator == "ge" else actual < expected if operator == "le" else False
                )
                if not satisfied:
                    status = "fail"
                    reason = "A materialized value violates an explicit numerical requirement."
                    parsed.setdefault("issues", []).append({
                        "code": "numeric_requirement_mismatch", "severity": "major", "file": file_name,
                        "message": f"{comparison.get('scope')}: observed {actual} must be {operator} {expected} {comparison.get('unit')}.",
                        "recommendation": "Regenerate the affected asset to satisfy this original-request bound.",
                        "comparison": comparison,
                    })
    if not evidence:
        raise ValueError("No evidence excerpt matches the supplied file content. Use an exact non-comment excerpt and its inventory path, or request missing content with needs_evidence.")
    conditions = parsed.get("condition_checks") or []
    if status == "pass":
        if issues:
            raise ValueError("A pass cannot contain unresolved issues; resolve them or return fail.")
        if not conditions:
            raise ValueError("condition_checks is missing: link each original-request condition to zero-based evidence_indices.")
        for index, item in enumerate(conditions):
            if (not isinstance(item, dict) or not str(item.get("condition") or "").strip()
                    or not isinstance(item.get("evidence_indices"), list) or not item["evidence_indices"]):
                raise ValueError(f"condition_checks[{index}] needs condition and a nonempty evidence_indices list.")
            if not any(type(i) is int and i in evidence_indices for i in item["evidence_indices"]):
                details = {i: evidence_errors.get(i, "unknown evidence index")
                           for i in item["evidence_indices"] if type(i) is int}
                raise ValueError(f"condition_checks[{index}] has no verified evidence: {details}. Quote shorter exact lines or request missing evidence; do not repeat the rejected excerpt.")
    return {
        "requirement_id": requirement_id,
        "status": status,
        "evidence": evidence,
        "reason": reason,
        "condition_checks": [
            {"condition": item["condition"],
             "evidence_indices": [evidence_indices[i] for i in item["evidence_indices"] if type(i) is int and i in evidence_indices]}
            for item in conditions if isinstance(item, dict)
            and isinstance(item.get("evidence_indices"), list) and item.get("condition")
        ],
    }


async def review_delivery_against_authority(
    state: State,
    *,
    files: dict[str, str],
    preprocessing_request: dict[str, Any],
    work_order: dict[str, Any],
    plan_context: dict[str, Any],
    deterministic_review: dict[str, Any],
    package_dir: str | Path | None = None,
    producer_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Review staged assets, resolving evidence requests before asset revision."""
    requirements = _requirement_list(preprocessing_request, work_order, plan_context)
    requirement_id = requirements[0]["requirement_id"]
    client = state.hook_state.get("_data_planning_llm_client")
    if client is None or not callable(getattr(client, "chat", None)):
        return _reviewer_error(
            "authority_reviewer_unavailable",
            "The model authority reviewer is unavailable; semantic request alignment was not verified.",
        )
    if callable(getattr(client, "background_copy", None)):
        client = client.background_copy()
    delivery_evidence = _file_evidence(files, package_dir=package_dir)
    available_files = set(delivery_evidence)
    source_evidence = _file_evidence(producer_evidence or {}, total_limit=20000)
    proposed_assets = {
        item["id"] for item in plan_context.get("required_deliverables") or []
        if isinstance(item, dict) and item.get("output_path_is_explicit") is False
    }
    pending_bindings = [
        issue for issue in deterministic_review.get("issues") or []
        if issue.get("code") == "required_asset_unfulfilled"
        and issue.get("deliverable_id") in proposed_assets
    ]
    payload = {
        "preprocessing_request": _review_request_context(preprocessing_request),
        "authority_requirements": requirements,
        # Route/identity hints stay outside locked authority. An inferred
        # script, filename or report syntax cannot create a caller obligation.
        "delivery_plan": [{
            key: item.get(key) for key in (
                "id", "declared_output_path", "format", "scientific_role",
                "delivery_required", "output_path_is_explicit",
            )
        } for item in plan_context.get("required_deliverables") or [] if isinstance(item, dict)],
        "quality_references": (
            (plan_context.get("requirement_analysis") or {}).get("reference_evidence") or []
        ),
        "delivery_inventory": [
            {
                "path": str(name),
                "size_bytes": len(str(content or "").encode("utf-8", errors="replace")),
            }
            for name, content in files.items()
        ],
        "delivery_files": delivery_evidence,
        "producer_evidence": source_evidence,
        "inspected_input_sources": [
            {key: item.get(key) for key in ("path", "requested_path", "size_bytes", "sha256")}
            for item in (plan_context.get("requirement_analysis") or {}).get("local_asset_inventory") or []
            if isinstance(item, dict)
        ],
        "deterministic_review": {
            "status": "pending_asset_bindings" if pending_bindings else deterministic_review.get("status"),
            "issues": [issue for issue in deterministic_review.get("issues") or [] if issue not in pending_bindings],
            "checks": {key: value for key, value in (deterministic_review.get("common_checks") or {}).items()
                       if key not in {f"required_asset:{issue.get('deliverable_id')}" for issue in pending_bindings}},
        },
        "pending_asset_bindings": [issue.get("deliverable_id") for issue in pending_bindings],
    }
    async def request_review(review_payload: dict[str, Any]) -> dict[str, Any]:
        response = await client.chat(
            [
                LLMMessage(role="system", content=_SYSTEM_PROMPT),
                LLMMessage(role="user", content=json.dumps(review_payload, ensure_ascii=False, default=str)),
            ],
            tools=[_REVIEW_TOOL],
            max_tokens=8192,
            temperature=0.1,
        )
        if isinstance(response.usage, dict):
            state.tokens_used += int(response.usage.get("total_tokens") or 0)
        for call in response.tool_calls or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or function.get("name") != "submit_authority_review":
                continue
            return _extract_object(function.get("arguments") or "")
        return _extract_object(response.content or "")

    parsed: dict[str, Any] = {}
    check: dict[str, Any] | None = None
    errors: list[str] = []
    review_attempts = 0
    seen_protocol_failures: set[str] = set()
    while True:
        review_attempts += 1
        try:
            parsed = await request_review({
                **payload,
                **({
                    "repair_instruction": (
                        "Correct the previous_review using the same submit_authority_review schema. "
                        "Preserve verified evidence and substantive findings; repair only the reported protocol problem. "
                        + (errors[-1] if errors else "")
                    ),
                } if errors else {}),
            })
            if parsed.get("status") == "needs_evidence":
                requested = sorted(set(
                    name for name in parsed.get("requested_files") or []
                    if isinstance(name, str) and name in available_files | set(source_evidence)
                ))
                signature = "evidence:" + json.dumps(requested)
                if not requested:
                    errors.append("The reviewer requested no materialized file from the delivery inventory.")
                    break
                if signature in seen_protocol_failures:
                    repeated = "repeated:" + signature
                    if repeated in seen_protocol_failures:
                        errors.append(
                            "The reviewer repeated an evidence request after the requested files were supplied."
                        )
                        break
                    seen_protocol_failures.add(repeated)
                    payload["previous_review"] = parsed
                    errors.append(
                        "The requested files are already present in delivery_files. Decide pass or fail from "
                        "the supplied materialized evidence; do not request the same files again."
                    )
                    state.append_transcript(
                        "authority_review_repair", reason="repeated_evidence_request", files=requested
                    )
                    continue
                seen_protocol_failures.add(signature)
                focused = {}
                for name in requested:
                    if name in files:
                        focused[name] = files[name]
                    elif name in (producer_evidence or {}):
                        focused[name] = producer_evidence[name]
                    elif package_dir:
                        root = Path(package_dir).resolve()
                        path = (root / name).resolve()
                        if path.is_relative_to(root) and path.is_file():
                            focused[name] = path.read_text(encoding="utf-8", errors="replace")
                # Re-sample the requested originals with their own budget.
                # Feeding already-truncated excerpts back through the sampler
                # used to starve large solver decks and made a repeated request
                # look like missing external evidence.
                focused_evidence = _file_evidence(focused, total_limit=120000)
                delivery_evidence = {**delivery_evidence, **focused_evidence}
                payload["delivery_files"] = delivery_evidence
                payload["evidence_focus"] = {
                    "files": requested,
                    "instruction": (
                        "These requested files have now been re-read with a dedicated evidence budget. "
                        "Return a pass/fail verdict; request only a different materialized file if essential."
                    ),
                }
                state.append_transcript("authority_review_repair", reason="requested_evidence", files=requested)
                continue
            check = _verdict_check(parsed, requirement_id, {**source_evidence, **delivery_evidence})
            binding_error = ""
            bound = {
                item.get("asset_id") for item in parsed.get("asset_bindings") or []
                if isinstance(item, dict) and isinstance(item.get("paths"), list)
                and item["paths"] and all(isinstance(path, str) and path in available_files for path in item["paths"])
            }
            unresolved_proposals = {
                item.get("deliverable_id") for item in parsed.get("issues") or []
                if item.get("code") == "required_asset_unfulfilled"
                and item.get("deliverable_id") in proposed_assets - bound
            }
            signature = "unresolved_proposals:" + json.dumps(sorted(unresolved_proposals))
            if check is not None and unresolved_proposals and signature not in seen_protocol_failures:
                seen_protocol_failures.add(signature)
                payload["previous_review"] = parsed
                state.append_transcript("authority_review_repair", reason="unresolved_asset_bindings",
                                        asset_ids=sorted(unresolved_proposals))
                errors.append(
                    "Resolve the proposed asset names against actual inventory content before requesting regeneration. "
                    "Return bindings for equivalent content even if other requirements fail. If content really is absent, "
                    "cite the missing original-request content, not the planner's proposed filename."
                )
                continue
            if check is not None and check["status"] == "pass":
                unresolved = {
                    item.get("deliverable_id") for item in deterministic_review.get("issues") or []
                    if item.get("code") == "required_asset_unfulfilled"
                }
                if not unresolved <= bound:
                    check = None
                    binding_error = f"Missing inventory bindings for required asset IDs: {sorted(unresolved - bound, key=str)}. Bind existing content or report a genuine defect."
                elif not any(item["file"] in available_files for item in check["evidence"]):
                    check = None
                    binding_error = "Producer configuration alone is not proof of delivery; inspect actual output evidence."
            if check is not None:
                break
            error = binding_error
        except Exception as exc:
            check = None
            error = f"{type(exc).__name__}: {str(exc)[:1200]}"
        signature = error
        payload["previous_review"] = parsed
        errors.append(error)
        if signature in seen_protocol_failures:
            break
        seen_protocol_failures.add(signature)
        state.append_transcript(
            "authority_review_repair",
            attempt=review_attempts + 1,
            reason="invalid_review_response",
            error=error,
        )
    if check is None:
        return _reviewer_error(
            "authority_reviewer_failed",
            "The authority review made no further protocol progress: " + "; ".join(errors),
        )

    issues = [
        {
            **item,
            "severity": str(item.get("severity") or "critical").lower(),
            "repairable": item.get("repairable") is not False,
            "requirement_id": requirement_id,
        }
        for item in parsed.get("issues") or []
        if isinstance(item, dict)
    ]
    for issue in issues:
        if issue.get("source_file") not in source_evidence:
            issue.pop("source_file", None)
    status = check["status"]
    if status == "fail" and not issues:
        issues.append({
            "code": "authority_requirement_not_satisfied",
            "severity": "critical",
            "file": "",
            "message": check["reason"],
            "recommendation": "Regenerate only the cited assets while preserving the locked authority.",
            "repairable": True,
            "requirement_id": requirement_id,
        })
    return {
        "review_type": "model_authority_alignment",
        "status": status,
        "summary": check["reason"],
        "requirement_checks": [check],
        "issues": issues,
        "asset_bindings": parsed.get("asset_bindings") or [],
        "model": getattr(client, "model", "") or "inherited_data_model",
        "review_attempts": review_attempts,
        **({"revision_contract": revision_contract(issues)} if status == "fail" else {}),
    }


def combine_reviews(
    deterministic_review: dict[str, Any],
    authority_review: dict[str, Any],
) -> dict[str, Any]:
    """Keep deterministic and semantic evidence separate while producing one verdict."""
    issues = [
        *(deterministic_review.get("issues") or []),
        *(authority_review.get("issues") or []),
    ]
    passed = (
        deterministic_review.get("status") == "pass"
        and authority_review.get("status") == "pass"
    )
    result = {
        **deterministic_review,
        "status": "pass" if passed else "fail",
        "issues": issues,
        "authority_review": authority_review,
    }
    if not passed:
        result["revision_contract"] = revision_contract(issues)
    return result
