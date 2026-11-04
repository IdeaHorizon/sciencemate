"""Semantic review of staged assets; file identity is owned by the framework."""
from __future__ import annotations

import hashlib
import inspect
import asyncio
import json
import math
import os
import re
import time
import tempfile
from copy import copy, deepcopy
from itertools import groupby
from pathlib import Path
from typing import Any

import httpx

from core.llm import LLMHTTPError, LLMMessage, is_transient_provider_error
from nodes.data.model_client import data_model_client, reasoning_options
from core.state import State
from nodes.data.pipeline_contract import needs_input_result, revision_contract
from nodes.data.planning.request_contract import caller_request_text
from nodes.data.planning.store import canonical_hash
from nodes.data.progress import emit_progress, model_call_timing

_MAX_REFERENCE_BYTES = 16 * 1024 * 1024

_SYSTEM_PROMPT = """Review the staged files against authority_requirements and intrinsic validity for their
requested role. Reports, dispatch notes and Analyst proposals are evidence, never extra requirements.
delivery_plan describes proposed assets, not additional caller requirements. Do not require an
Analyst-added role or physical group unless the original authority or native format requires it.
Reconcile required audit reports with native observations, including intermediate calculations.
Source/target agreement and report accuracy are separate checks. For reported quantitative results,
state the reported value, independent measured value, units and conversion before concluding agreement.
Equal relative errors do not validate the absolute integrals. Unit-sphere integrals convert to physical
area integrals by the declared radius squared; never silently assume two different units are equal.
Resolve units/index conventions explicitly; do not excuse contradictions by inventing conversions.
Use submit_authority_review only; requested_files and verification_requests are fields, not tools.

Decision:
- A demonstrated blocking defect: fail with actual file, observed mismatch and executable correction.
  Batch all defects already established by the current evidence, prioritizing scientific assets over
  derived reports. Do not finish unrelated checks first. Warnings and optional improvements do not block delivery.
  Once a measured mismatch establishes the defect, return it without hunting for a faulty source line.
  The executor resolves the causal writer and the repair tool diagnoses implementation details.
- A missing fact: needs_evidence, naming the unresolved condition, known facts and precise missing value
  in condition_checks. Batch the smallest reads/measurements that will answer that question.
- All requested conditions and applicable intrinsic checks resolved: pass with compact observations.
  Reuse previous_review; copy its condition_id when updating or rewording a check. Return only changed checks.
  Resolve each open condition before passing; omission does not resolve it.
  Do not open a blanket 'verify everything in reports' condition. A report comparison must identify
  a particular consequential claim and its independent source; once answered, close that question.
- needs_input is for an essential caller decision, never for defaults the caller delegated.
  A blocking note or request for missing input cannot satisfy a requested scientific asset.
  If the caller's indispensable source is absent and cannot be substituted, return needs_input;
  do not pass a report describing the blockage as completed scientific delivery.

Scientific evidence:
Check geometry, units/reference frames, bounds, counts, active configuration, assignments and interfaces.
Preserve explicit tolerances, quantifiers and requested paths. Do not impose extra resolution, element
families, solver runs or optional deliverables. Native discrete placement may disclose rounding only
when the caller has not required exact placement. Distributed loads need their weights/integrals.
Materialized settings, measured geometry and operational validity are distinct. Input echoes, comments,
file names, hashes and exit zero alone do not prove physical correctness. Use native measurements within
their stated scope; measured_delivery binds a producer's native measurements to the exact
delivered bytes by SHA-256, so copying into staging does not require another parser. A missing or
mismatched binding does not establish that equivalence. Do not repeat already measured facts.
Versioned native_file_inspections own the schema checks within their stated scope. Check semantic
values and cross-file consistency; reopen a native result only with a concrete conflicting file value
or source rule. An unfamiliar group, XML element or legal default override is not itself missing evidence.
Local refinement requires measured adjacent-cell
sizes in refined and comparison regions. Source sizing fields, boundary point spacing and input-derived
threshold booleans cannot establish achieved refinement. Request that measurement before marking pass.
Sizing controls are targets, not per-edge guarantees unless explicitly required. CAD control points and
coordinate-system origins do not establish entity bounds; use native entity measurements and their tolerance.
Report consistency checks prove only the named fields agree with producer receipts, not scientific
validity. Reuse those checks and inspect any uncovered consequential claim or actual contradiction.
Historical planning proposals explicitly marked unadopted are not execution claims.
For application configuration files establish legal groups/keys and cross-file relationships using an installed
target reader or official version-matched references, and cite that evidence in condition_checks.
Generic syntax parsing and similar field names do not prove application-schema legality or equal counts.
For solver decks, verify that element formulations and procedure keywords belong to the selected solver;
a shared file extension or an INP mesh read-back does not prove cross-solver compatibility.
Any blocking format/metadata claim needs a native validator or applicable official rule. Distinguish
mandatory fields from optional attributes, inherited metadata and recommendations before requesting edits.
Check indexed cardinality, effective values and applicable defaults/inheritance. Configuration-only delivery can disclose external runtime prerequisites;
ready-to-run requests still require them. Never substitute a preferred convention for an equivalent one.

Evidence access:
Every turn contains this window and previous_review. Keep measured values and the precise remaining gap
in observations. No need to repeat passing checks or reread their files without a concrete contradiction.
Plan measurements against remaining_review_budget: batch related unresolved conditions sharing a parser
into one verification (for example bounds, connectivity, regions and load integrals). Do not spend a
verification printing keywords or one coordinate bound at a time. At zero, use acquired evidence and
requested_files; if an essential measurement remains unavailable, state that gap without claiming pass.
Use exact inventory FILE keys, JSON pointers or line ranges. Large evidence_file references remain readable.
For pending_evidence omit requested_files to continue, or use next_requests. String offsets retain their
JSON pointer/line selector. Missing facts need a relevant source or measurement; reading alone is not approval. Numeric records may be projected; do not infer omitted values.
Use measure_mesh_regions for Gmsh topology and tetrahedral measurements (MSH2/MSH4, ASCII/binary),
not a handwritten MSH parser. Optional regions measure surface-face areas/edge sizes in XYZ centroid
boxes; select one Z plane of a 2D extrusion and separate refined/reference regions.
Use measure_netcdf_conservation for a rectilinear latitude/longitude regrid with the same variable name
and units: specify source_file, target_file and variable, and include both exact paths in files. It computes
independent spherical integrals with explicit coverage/missing exclusions; do not rewrite it as Python.
execute_python is for computation using installed readers, not printing files or dumping report fields.
Print only the requested computed comparison, its units/scope and any mismatch. input_files is an injected
dict of exact inventory keys to read-only paths: Path(input_files[key]).read_text(). No imports of
input_files, filesystem discovery, installs, downloads, simulations or asset edits. Tool failure is not
an asset defect. Use requested_files for text. web_search discovers sources; web_fetch/read_file retrieve
applicable version-matched definitions. Use discovered source URLs, not guessed manual page numbers.
Source discovery/retrieval may use files=[]; computations must
bind their actual inventory inputs. read_file searches literal substrings in cached text using
path=inventory key. requested_files reads exact
sections. Excerpts are not a verdict; an absent search term does not prove an absent schema field.

Repair and delivery:
Resolve pending_asset_bindings to actual delivered files. Proposed paths are not caller requirements;
explicit paths are. delivery_destination is the request-scoped publication directory.
For issues, file is where observed; step_id/source_file identify the causal producer. producer_map.files
are current delivered ownership; source_files are lineage only unless the defect is actually there.
Use derived_report for inconsistent tool-computed reports, artifact_content for editable content, and
producer_controls for wrong producer settings. Preserve valid upstream assets; never change scientific
parameters merely to fix a report. Do not regenerate for a reviewer protocol failure.
Use plan_structure when a requested delivery has no production step (for example only a generator
script exists, with no step executing it). Name the missing delivery in deliverable_id and file;
explain the missing step against the original request. Omit step_id/source_file: rewriting an existing
script cannot supply a missing execution step. If a producer exists but produced bad content, use
artifact_content or producer_controls instead.
No chain of thought; return concise conclusions and evidence."""

_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_authority_review",
        "description": "Return concise observations, actionable defects, or a scoped evidence request.",
        "parameters": {
            "type": "object", "properties": {
                "status": {"type": "string", "enum": ["pass", "fail", "needs_evidence", "needs_input"]},
                "summary": {"type": "string"},
                "condition_checks": {"type": "array", "items": {
                    "type": "object", "properties": {
                        "condition": {"type": "string", "description": "Describe the checked caller condition or target-format/role validity; related checks may be grouped."},
                        "condition_id": {"type": "string", "description": "For an existing check, copy its ID from previous_review even when changing its description. Omit for a new check."},
                        "observation": {"type": "string"},
                        "status": {"type": "string", "enum": ["pass", "fail", "needs_evidence"]},
                        "files": {"type": "array", "items": {"type": "string"}},
                    }, "required": ["condition", "status", "files", "observation"],
                }},
                "issues": {"type": "array", "items": {
                    "type": "object", "properties": {
                        **{key: {"type": "string"} for key in
                           ("code", "deliverable_id", "message", "recommendation")},
                        "file": {"type": "string", "description": "Inventory file where the defect is observed, or missing delivery path for plan_structure."},
                        "step_id": {"type": "string", "description": "Existing producer that controls the demonstrated cause, not necessarily the observed file's last writer."},
                        "source_file": {"type": "string", "description": "Existing source that must be edited to fix this cause; omit for mere lineage."},
                        "repair_kind": {"type": "string", "enum": ["artifact_content", "producer_controls", "derived_report", "plan_structure"]},
                        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                    }, "required": ["file", "message", "recommendation"],
                }},
                "asset_bindings": {"type": "array", "items": {
                    "type": "object", "properties": {
                        "asset_id": {"type": "string"},
                        "paths": {"type": "array", "items": {"type": "string"}},
                    }, "required": ["asset_id", "paths"],
                }},
                "requested_files": {"type": "array", "items": {"anyOf": [
                    {"type": "string"}, {"type": "object", "properties": {
                        **{key: {"type": "string"} for key in ("file", "json_pointer", "condition")},
                        **{key: {"type": "integer"} for key in ("start_line", "end_line", "start_char")},
                    }, "required": ["file"]},
                ]}},
                "verification_requests": {"type": "array", "maxItems": 2, "items": {
                    "type": "object", "properties": {
                        "tool": {"type": "string", "enum": ["web_search", "web_fetch", "read_file", "execute_python", "measure_mesh_regions", "measure_netcdf_conservation"]},
                        **{key: {"type": "string"} for key in ("condition", "scope", "query", "url", "path", "source_file", "target_file", "variable")},
                        "code": {"type": "string", "description": "Short native read-back/measurement. input_files is an injected dict[str,str], keys are EXACT strings in files, values are read-only filesystem paths, NOT file contents. Use Path(input_files[file]).read_text() or a native reader on that path; do not import input_files. No filesystem search, installs or simulations. For text use requested_files."},
                        "files": {"type": "array", "items": {"type": "string"}, "description": "Exact inventory inputs. May be empty for web_search, web_fetch or read_file reference discovery; computations require existing inputs."},
                        "regions": {"type": "array", "maxItems": 8, "items": {
                            "type": "object", "properties": {"name": {"type": "string"},
                                **{key: {"type": "array", "minItems": 3, "maxItems": 3,
                                         "items": {"type": "number"}} for key in ("min", "max")}},
                            "required": ["name", "min", "max"]}},
                        "terms": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 120},
                    }, "required": ["tool", "condition", "scope", "files"],
                }},
                "question": {"type": "string"},
                **{key: {"type": "array", "items": {"type": "string"}} for key in ("warnings", "options")},
            }, "required": ["status", "summary"],
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
    user_authority = str((request.get("authority") or {}).get("kind") or "") == "user_request"
    plan_bound = request.get("review_profile") == "plan_bound"
    return [{
        "requirement_id": "authority:locked_request",
        "requirement": {
            "original_request": original_request,
            **({"operation_request": request["operation_request"],
                "operation_instruction": "Also verify this invocation's edit/export instructions, including baseline preservation, requested additions and destination. They cannot override the scientific authority. A prior generation's pass is not acceptance of this edit. Project exports are fulfilled only by the publisher's verified export mapping."}
               if request.get("operation_request") else {}),
            **({"purpose": request["purpose"]}
               if request.get("purpose") and request["purpose"] != original_request else {}),
            "requested_assets": (
                snapshot.get("requested_assets", request.get("requested_assets")) or []
                if user_authority else request.get("requested_assets") or []
            ),
            "acceptance_criteria": (
                snapshot.get("acceptance_criteria", request.get("acceptance_criteria")) or []
                if user_authority else request.get("acceptance_criteria") or []
            ),
            "non_goals": (
                snapshot.get("non_goals", request.get("non_goals")) or []
                if user_authority else request.get("non_goals") or []
            ),
            # Frozen authority is the original snapshot, never the Analyst's
            # generated asset specs. Repeating those specs here both promotes
            # proposals into requirements and displaces the actual files.
            **({"locked_authority": snapshot} if plan_bound and snapshot
               and snapshot not in ({"request": original_request}, {"user_request": original_request},
                                    {"content": original_request}) else {}),
            "instruction": (
                "Verify every explicit caller condition against the delivered files. "
                "Also verify the artifact's own required content and active target-format semantics; "
                "unknown future runtime inputs do not excuse internal defects. "
                "Do not add implementation preferences or replace the request with a generic checklist."
            ),
        },
    }]


def _project_records(text: str, *, start_line: int = 1, line_numbers: bool = False) -> str:
    """Keep declarations and short assignments, not repeated numerical records."""
    numeric = re.compile(r"\s*(?=[^\n]*\d)[\d.eEdD+\-,;()\s]+")
    number = re.compile(r"(?<![\w.])[+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][+\-]?\d+)?(?![\w.])")

    def record_key(row: tuple[int, str]) -> str | None:
        line = row[1]
        if numeric.fullmatch(line):
            return "numeric"
        template, count = number.subn("#", line)
        return " ".join(template.split()) if count >= 3 else None

    projected = []
    for key, rows in groupby(enumerate(text.splitlines(keepends=True), start_line), key=record_key):
        block = list(rows)
        if key and len(block) > 8:
            projected.extend(f"L{index}: {line}" if line_numbers else line for index, line in block[:2])
            projected.append(f"\n[... L{block[2][0]}-L{block[-3][0]}: {len(block) - 4} numeric record lines omitted; use native measurements ...]\n")
            block = block[-2:]
        projected.extend(f"L{index}: {line}" if line_numbers else line for index, line in block)
    return "".join(projected)


def _file_evidence(
    files: dict[str, Any],
    *,
    package_dir: str | Path | None = None,
    total_limit: int = 60000,
    overview: bool = False,
    line_numbers: bool = False,
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

    compacted = set()
    if overview:
        # Keep declarations and reports, not thousands of coordinate/index rows.
        # This is a model-context projection only; originals remain available
        # to deterministic validators and the existing needs_evidence reader.
        for name, content in candidates.items():
            text = str(content or "")
            sampled = isinstance(files.get(name), dict) and files[name].get("sampled")
            candidates[name] = _project_records(text, line_numbers=line_numbers and not sampled)
            if "numeric record lines omitted; use native measurements" in candidates[name]:
                compacted.add(name)
    elif line_numbers:
        for name, content in candidates.items():
            if not (isinstance(files.get(name), dict) and files[name].get("sampled")):
                candidates[name] = "".join(f"L{index}: {line}" for index, line in
                                           enumerate(str(content or "").splitlines(keepends=True), 1))

    # Read small files fully before sampling large ones. Reserve a fair share
    # for every readable file, irrespective of discipline or directory name.
    ordered = sorted(candidates.items(), key=lambda item: (overview and item[0] in compacted, len(item[1]), item[0]))
    unread = sum(bool(content) for _, content in ordered)
    for name, content in ordered:
        text = str(content or "")
        size = sizes.get(str(name), len(text.encode("utf-8", errors="replace")))
        if remaining <= 0 or not text or (overview and len(text) > 12000):
            if overview:
                # The review inventory already records existence and size.
                # Do not spend its input budget repeating omission notices.
                continue
            evidence[str(name)] = (
                "[materialized file; content omitted from model context; "
                f"size={size} bytes; request relevant content if its property is not established by supplied checks]"
            )
            continue
        # Preserve small configurations and native receipts in full. Dividing
        # their budget by large mesh/data files used to cut up the very
        # measurements the reviewer needed to cite, causing protocol retries.
        limit = min(remaining, len(text)) if len(text) <= 12000 else remaining // max(unread, 1)
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
            sampled = name in compacted or (isinstance(files.get(name), dict) and files[name].get("sampled"))
            evidence[str(name)] = f"[materialized file excerpt; size={size} bytes]\n{text}" if sampled else text
        remaining -= len(excerpt)
    return evidence


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
        "recommendation": "Preserve staged assets and the approved work order. Resume only review after the reviewer service/protocol problem is resolved; do not regenerate assets or replay exhausted retries.",
        "repairable": False,
    }]
    return {
        "review_type": "model_authority_alignment",
        "status": "error",
        "outcome": "externally_blocked",
        "failure_category": "review_protocol_error",
        "summary": message,
        "requirement_checks": [],
        "issues": issues,
    }

def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _status(value: Any) -> str:
    name = str(value or "").strip().lower()
    return {"satisfied": "pass", "approved": "pass", "approve": "pass", "passed": "pass",
            "unsatisfied": "fail", "revise": "fail", "failed": "fail"}.get(name, name)


def _completed_observations(review: dict[str, Any]) -> dict[str, Any]:
    """Keep observations on unchanged assets; unresolved findings are not passes."""
    checks = [{**item, "condition_id": item.get("condition_id") or canonical_hash(item["condition"])[:16]}
              for item in review.get("condition_checks") or [] if isinstance(item, dict) and item.get("condition")
              and item.get("observation") and _status(item.get("status")) in {"pass", "fail", "needs_evidence"}]
    requests = {"files": review.get("requested_files") or [],
                "verifications": [{key: item[key] for key in ("condition", "scope", "files") if key in item}
                                  for item in review.get("verification_requests") or [] if isinstance(item, dict)]}
    intent = ({"question": str(review.get("summary") or "")[:800], **requests}
              if any(requests.values()) else review.get("last_evidence_request"))
    return {**({"condition_checks": checks} if checks else {}),
            **({"asset_bindings": review["asset_bindings"]} if review.get("asset_bindings") else {}),
            **({"last_evidence_request": intent} if intent else {})}


def _reference_excerpt(text: str, request: dict[str, Any], max_chars: int = 4000) -> dict[str, Any]:
    """One bounded literal-term reader for local and retrieved definitions."""
    terms = request.get("terms") or []
    if not isinstance(terms, list) or len(terms) > 20 or any(not isinstance(term, str) or not term for term in terms):
        raise ValueError("Reference terms must be at most 20 nonempty literal strings.")
    start, limit = int(request.get("offset", 0)), int(request.get("limit", 80))
    if start < 0 or limit < 1:
        raise ValueError("Reference offset must be nonnegative and limit positive.")
    limit = min(limit, 120)
    lines = text.splitlines()
    hits = [[index for index, line in enumerate(lines) if term.casefold() in line.casefold()] for term in terms]
    selected = (sorted({near for group in hits for index in group
                        for near in range(max(start, index - 3), min(len(lines), index + 4))})
                if terms else list(range(start, len(lines))))
    first_matches = {term: next((index + 1 for index in group if index >= start), None)
                     for term, group in zip(terms, hits)}
    context_budget = max_chars - len(_encode(first_matches))
    # offset is a source-line offset, as in the shared reader, not an index
    # into matching lines. Mixing those meanings silently returned empty
    # evidence when a reviewer asked for a known later section.
    page: list[str] = []
    continuation = None
    for index in selected[:limit]:
        line = f"L{index + 1}: {lines[index]}"
        if len("\n".join([*page, line])) > context_budget:
            if not page:
                count = max(1, context_budget - len(f"L{index + 1}: "))
                page.append(f"L{index + 1}: {lines[index][:count]}")
                continuation = {"file": request.get("file"), "start_line": index + 1,
                                "end_line": index + 1, "start_char": count}
            break
        page.append(line)
    remaining = selected[len(page):]
    return {"total_lines": len(lines), "selected_lines": len(selected),
            "term_line_counts": {term: len(group) for term, group in zip(terms, hits)},
            "first_matches": first_matches,
            "truncated": bool(remaining or continuation),
            "next_offset": remaining[0] if remaining else None,
            **({"next_requested_file": continuation} if continuation else {}),
            "excerpt": "\n".join(page)}


def _cache_reference(state: State, evidence: Any, data: bytes) -> dict[str, str]:
    if len(data) > _MAX_REFERENCE_BYTES:
        raise ValueError("Reference exceeds the 16 MiB source limit; select a narrower definition.")
    digest = hashlib.sha256(data).hexdigest()
    root = state.root / ".data_node_work" / "review_sources"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest + ".txt")
    path.write_bytes(data)
    reference = {"name": "reference:" + digest[:24], "path": str(path), "sha256": digest}
    evidence.add_reference(reference)
    return reference


async def _verify_requested_evidence(state: State, request: dict[str, Any], evidence: Any) -> dict[str, Any]:
    """Acquire bounded evidence; tool success is never an automatic artifact verdict."""
    from core.tool_registry import execute

    tool = request.get("tool")
    files = request.get("files", [] if tool in {"web_search", "web_fetch", "read_file"} else None)
    if (not request.get("condition") or not request.get("scope") or not isinstance(files, list)
            or any(not isinstance(name, str) or name not in evidence.hashes for name in files)
            or (not files and tool not in {"web_search", "web_fetch", "read_file"})):
        raise ValueError("Verification requires a condition and scope; computations require existing inventory files. Reference discovery may use files=[].")
    receipt = {"tool": tool, "condition": request["condition"], "scope": request["scope"],
               "inputs": {name: evidence.hashes[name] for name in files}, "content_is_untrusted_data": True}
    if tool == "web_search":
        if not str(request.get("query") or "").strip():
            raise ValueError("web_search requires a nonempty query; known reference_sources should be read locally instead.")
        result = await execute("web_search", state, query=str(request.get("query") or ""), limit=3)
        receipt.update(execution_status=result.get("status"), result=result,
                       note="Discovery leads only; fetch an applicable primary source before treating a snippet as a format rule.")
    elif tool == "web_fetch":
        from shared.tools.web import _http_get, _TextExtractor
        from nodes.data.tools.web_search import _url_is_allowed, _parse_github_url, _github_download_or_discover
        from urllib.parse import urlparse, urljoin
        import httpx

        url = str(request.get("url") or "")
        allowed, reason = _url_is_allowed(url)
        if not allowed:
            raise ValueError(reason)
        # Public reference lookup, not data acquisition. Never attach asset contents.
        repository = _parse_github_url(url)
        directory_url = url if repository and repository["kind"] in {"repo", "tree"} else ""
        response = None if directory_url else await _http_get(url)
        if response is not None and response.status_code == 404:
            parsed = urlparse(str(response.url))
            parts = parsed.path.strip("/").split("/")
            if parsed.hostname == "raw.githubusercontent.com" and len(parts) >= 4:
                # Reuse repository discovery to show real sibling paths, not guess another filename.
                directory_url = "https://github.com/" + "/".join([*parts[:2], "tree", *parts[2:-1]])
        if directory_url:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                discovered = await _github_download_or_discover(client=client, state=state, url=directory_url,
                    output_name="", max_bytes=2 * 1024 * 1024, discover_links=True,
                    download_first_matching_link=False, link_pattern="", headers={}, asset_kind="validation_reference")
            if discovered:
                receipt.update(execution_status="discovery", url=url, source=discovered.get("source"),
                               candidates=(discovered.get("download_links") or [])[:60],
                               note="Select an actual definition path from this official repository listing; listing alone is not format validation.")
                return receipt
        if response is None:
            return {**receipt, "execution_status": "unavailable", "error": "Repository listing is unavailable; use another known source."}
        response.raise_for_status()
        allowed, reason = _url_is_allowed(str(response.url))
        if not allowed:
            raise ValueError(reason)
        # The download bound is not the model-context bound. Official manuals
        # often exceed 2 MiB; retain bounded source bytes and select excerpts
        # below instead of rejecting the very definition needed for repair.
        if len(response.content) > _MAX_REFERENCE_BYTES:
            return {**receipt, "url": str(response.url), "execution_status": "unavailable",
                    "error": "Reference exceeds the 16 MiB source limit; select a narrower source."}
        digest = hashlib.sha256(response.content).hexdigest()
        text = response.text
        if "html" in response.headers.get("content-type", "").lower():
            class ReferenceText(_TextExtractor):
                def handle_starttag(self, tag, attrs):
                    super().handle_starttag(tag, attrs)
                    href = dict(attrs).get("href") or ""
                    target = urljoin(str(response.url), href)
                    if tag == "a" and not self._skip_depth and href and urlparse(target).scheme in {"http", "https"}:
                        self.chunks.append(f" [{target}] ")

            parser = ReferenceText()
            parser.feed(text)
            text = "\n".join(line.strip() for chunk in parser.chunks for line in chunk.splitlines() if line.strip())
        reference = _cache_reference(state, evidence, text.encode("utf-8"))
        receipt.update(execution_status="success", url=str(response.url),
                       source_sha256=digest, reference_file=reference,
                       **_reference_excerpt(text, {**request, "file": reference["name"]}),
                       note="Retrieved excerpt only, not a validator or a complete target schema; verify version and applicability.")
    elif tool == "read_file":
        path = request.get("path") or (files[0] if len(files) == 1 else None)
        inventory_path = path if path in evidence.hashes else next(
            (name for name, bound in evidence.paths.items() if str(bound) == path), None)
        if inventory_path:
            receipt.update(execution_status="success", readback_only=True,
                           **_reference_excerpt(evidence.text(inventory_path), {**request, "file": inventory_path}))
            receipt["inputs"][inventory_path] = evidence.hashes[inventory_path]
            state.append_transcript("authority_verification", **receipt)
            return receipt
        if not path:
            raise ValueError("read_file requires an inventory key or an external local reference path.")
        result = await execute("read_file", state, path=path,
                               offset=int(request.get("offset", 0)), limit=min(120, max(1, int(request.get("limit", 80)))))
        receipt.update(execution_status=result.get("status"),
                       result={key: result[key] for key in ("path", "content", "total_lines", "truncated", "error") if key in result})
        if len(str(receipt["result"].get("content", ""))) > 4000:
            receipt["result"].update(content=receipt["result"]["content"][:4000], truncated=True)
        resolved = Path(result.get("path") or path)
        if result.get("status") == "success" and resolved.is_file() and resolved.stat().st_size <= _MAX_REFERENCE_BYTES:
            reference = _cache_reference(state, evidence, resolved.read_bytes())
            receipt.update(reference_file=reference, source_sha256=reference["sha256"], url=resolved.resolve().as_uri())
    elif tool == "measure_mesh_regions":
        from nodes.data.tools.mesh_generator import read_gmsh_mesh_quality, _parse_gmsh_msh2_quality

        if len(files) != 1 or files[0] not in evidence.paths:
            raise ValueError("measure_mesh_regions needs one materialized Gmsh file.")
        path = evidence.paths[files[0]]
        with path.open("rb") as stream:
            ascii_msh2 = stream.read(64).splitlines()[:2] == [b"$MeshFormat", b"2.2 0 8"]
        reader = _parse_gmsh_msh2_quality if ascii_msh2 else read_gmsh_mesh_quality
        measured = await asyncio.to_thread(reader, str(path), surface_regions=request.get("regions"))
        receipt.update(execution_status="success", result=measured["surface_region_measurements"] if request.get("regions") else measured)
    elif tool == "measure_netcdf_conservation":
        from .netcdf_inspection import measure_conservation

        source, target = request.get("source_file"), request.get("target_file")
        if source not in files or target not in files or not request.get("variable"):
            raise ValueError("Conservation requires source_file/target_file bound in files and a variable name")
        try:
            receipt.update(execution_status="success", result=measure_conservation(
                evidence.paths[source], evidence.paths[target], request["variable"]))
        except (ImportError, ValueError, OSError, KeyError) as exc:
            receipt.update(execution_status="unsupported", error=str(exc))
    elif tool == "execute_python":
        code = str(request.get("code") or "")
        if not code.strip() or len(code) > 8000:
            raise ValueError("Verification code must be a short nonempty read-back/measurement (at most 8000 characters).")
        directory = state.root / ".data_node_work" / "review_checks"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            root = Path(temporary)
            work, snapshots = root / "work", root / "inputs"
            work.mkdir(); snapshots.mkdir()
            inputs = {}
            for index, name in enumerate(files):
                path = evidence.paths.get(name)
                if path is None:
                    path = snapshots / f"{index}_{Path(name).name}"
                    path.write_text(evidence.text(name), encoding="utf-8")
                inputs[name] = str(path.resolve())
                relative = Path(name)
                if not relative.is_absolute() and ".." not in relative.parts:
                    alias = work / relative
                    alias.parent.mkdir(parents=True, exist_ok=True)
                    alias.symlink_to(path.resolve())
            # Reuse the shared dispatcher, risk guard and mandatory networkless executor.
            # Only scratch is writable; the selected assets are read-only. No requirements/install.
            output_dir = Path(tempfile.mkdtemp(prefix=".streams-", dir=work))
            streams = {name: output_dir / (name + ".txt") for name in ("stdout", "stderr")}
            capture = "import sys as _data_review_sys\n" + "".join(
                f"_data_review_sys.{name} = open({str(path)!r}, 'w', encoding='utf-8')\n"
                for name, path in streams.items())
            result = await execute("execute_python", state,
                code=capture + "input_files = " + repr(inputs) + "\n" + code, cwd=str(work), timeout=60,
                _sandbox_writable_roots=[work], _sandbox_readonly_roots=list(inputs.values()))
            # The shared tool's tail is not the complete observation. Keep the
            # full bounded output before scratch cleanup, so paging can retrieve
            # early measurements without executing the same check again.
            for name, path in streams.items():
                if path.is_file():
                    if path.stat().st_size > _MAX_REFERENCE_BYTES:
                        raise ValueError("Verification output exceeds the evidence limit; print only the requested measurements.")
                    result[name] = path.read_text(encoding="utf-8", errors="replace")
        receipt.update(execution_status=result.get("status"),
                       code_sha256=hashlib.sha256(code.encode()).hexdigest(),
                       method=code, result={key: result[key] for key in
                           ("returncode", "stdout", "stderr", "stdout_tail", "stderr_tail", "error", "error_code", "question")
                           if key in result and (not key.endswith("_tail") or
                               key.removesuffix("_tail") not in result and result[key])})
    else:
        raise ValueError("Unsupported verification tool; use the declared verification_requests tools.")
    state.append_transcript("authority_verification", **receipt)
    return receipt


class _Evidence:
    """One materialized inventory, bounded views, and exact on-demand reads."""

    def __init__(self, files: dict[str, Any], sources: dict[str, Any], package_dir: str | Path | None):
        self.delivered = set(files)
        self.originals = {**sources, **files}
        self.paths: dict[str, Path] = {}
        self.hashes: dict[str, str] = {}
        self.aliases: dict[str, str] = {}
        self.inventory: list[dict[str, Any]] = []
        self.binary: set[str] = set()
        self.bulk: set[str] = set()
        root = Path(package_dir).resolve() if package_dir else None
        copies: dict[str, str] = {}
        for name in sorted(self.originals, key=lambda name: (name not in self.delivered, name)):
            path = (root / name).resolve() if root and name in self.delivered else Path(name)
            if path.is_file():
                if name in self.delivered and root and not path.is_relative_to(root):
                    raise ValueError(f"Unsafe delivery evidence path: {name}")
                self.paths[name] = path
                digest, size, count, last, binary = hashlib.sha256(), 0, 0, b"", False
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        count += chunk.count(b"\n")
                        last = chunk[-1:]
                        binary = binary or b"\x00" in chunk
            else:
                value = self.originals[name]
                data = str(value.get("content", "") if isinstance(value, dict) else value or "").encode("utf-8")
                digest, size = hashlib.sha256(data), len(data)
                count, last, binary = data.count(b"\n"), data[-1:], b"\x00" in data
            self.hashes[name] = digest.hexdigest()
            if binary:
                self.binary.add(name)
            entry = {"path": name, "size_bytes": size, "binary": binary,
                     "role": "delivery" if name in self.delivered else "producer"}
            if name not in self.delivered and isinstance(self.originals[name], dict) and self.originals[name].get("role") == "reference":
                entry["role"] = "reference"
            if not binary:
                entry["line_count"] = count + int(bool(last) and last != b"\n")
            original = copies.setdefault(self.hashes[name], name)
            if original != name:
                self.aliases[name] = original
                if name not in self.delivered:
                    continue
                entry["same_content_as"] = original
            self.inventory.append(entry)

    def canonical(self, name: Any) -> str:
        if not isinstance(name, str) or name not in self.hashes:
            raise ValueError(f"Unknown evidence path: {name}")
        return self.aliases.get(name, name)

    def add_verification(self, name: str, receipt: Any, *, role: str = "verification") -> str:
        text = _encode(receipt)
        self.originals[name] = text
        self.hashes[name] = hashlib.sha256(text.encode()).hexdigest()
        self.inventory.append({"path": name, "size_bytes": len(text.encode()), "role": role, "line_count": 1})
        # A generated verifier is fallible: its method and observations must be
        # inspected together, not presented as a trusted native verdict. The
        # same bounded reader handles receipts too large for the live window.
        # Compact native receipts must retain their actual definitions/methods:
        # the generic overview hides strings >800 chars even when they fit.
        if role == "native_measurements" and len(text) <= 16 * 1024:
            return text  # The assembled prompt still enforces the global budget.
        return self.overview({name}).get(name, "")

    def add_reference(self, record: dict[str, str]) -> None:
        name, path = record["name"], Path(record["path"])
        if name in self.hashes:
            return
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("Retrieved reference bytes changed; reacquire the source before using it.")
        self.paths[name] = path
        self.originals[name] = ""
        self.hashes[name] = record["sha256"]
        if b"\0" in data:
            self.binary.add(name)
        self.inventory.append({"path": name, "role": "reference", "size_bytes": len(data), "line_count": data.count(b"\n") + 1})

    def text(self, name: str) -> str:
        name = self.canonical(name)
        if name in self.binary:
            raise ValueError(f"{name} is binary; use native read-back measurements, not a text read.")
        if name in self.paths:
            return self.paths[name].read_text(encoding="utf-8", errors="replace")
        value = self.originals[name]
        return str(value.get("content", "") if isinstance(value, dict) else value or "")

    def overview(self, names: set[str] | None = None) -> dict[str, str]:
        shared: dict[str, str] = {}

        def project(value: Any, location: str) -> Any:
            if isinstance(value, str) and len(value) > 800 or isinstance(value, list) and len(value) > 16:
                return "[value omitted; request this JSON pointer]"
            if isinstance(value, (dict, list)) and len(_encode(value)) > 512:
                original = shared.setdefault(canonical_hash(value), location)
                if original != location:
                    return f"[same content at {original}]"
            if isinstance(value, dict):
                return {key: project(item, location + "/" + str(key).replace("~", "~0").replace("/", "~1"))
                        for key, item in value.items()}
            if isinstance(value, list):
                return [project(item, location + f"/{index}") for index, item in enumerate(value)]
            return value

        views = {}
        for entry in sorted(self.inventory, key=lambda item: (item["size_bytes"], item["path"])):
            name = entry["path"]
            if name in self.binary or name in self.aliases or names is not None and name not in names:
                continue
            text = self.text(name)
            lines = text.splitlines()
            numeric = sum(bool(re.fullmatch(r"\s*(?=[^\n]*\d)[\d.eEdD+\-,;()\s]+", line)) for line in lines)
            if numeric > len(lines) / 2:
                self.bulk.add(name)
            try:
                text = _encode(project(json.loads(text), name + "#"))
            except ValueError:
                text = re.sub(r"(?ms)^[ \t]*/\*.*?\*/[ \t]*\n?", lambda match: "\n" * match.group().count("\n"), text)
                # Keep explanatory comments; they can disclose input assumptions.
                text = _file_evidence({name: text}, total_limit=6000, overview=True, line_numbers=True).get(name, "")
                if "numeric record lines omitted; use native measurements" in text:
                    self.bulk.add(name)
            if text:
                views[name] = text
        return views

    @staticmethod
    def block_selector(name: str, block: dict[str, Any]) -> tuple[Any, ...]:
        key = (name, block["json_pointer"]) if "json_pointer" in block else (name, block["start_line"], block["end_line"])
        return (*key, block["start_char"], block["end_char"]) if "start_char" in block else key

    @staticmethod
    def covered(key: tuple[Any, ...], selectors: list[tuple[Any, ...]]) -> bool:
        for prior in selectors:
            if prior[0] != key[0]:
                continue
            if len(prior) == 2 and len(key) in {2, 4} and (
                    prior[1] == key[1] or not prior[1] or key[1].startswith(prior[1] + "/")):
                return True
            if len(prior) == 3 and len(key) in {3, 5} and prior[1] <= key[1] and prior[2] >= key[2]:
                return True
            if len(prior) in {4, 5} and prior[:-2] == key[:-2] and prior[-2] <= key[-2] and prior[-1] >= key[-1]:
                return True
        return False

    @staticmethod
    def pointer_value(value: Any, pointer: str) -> Any:
        parent = ""
        for token in pointer.split("/")[1:]:
            key = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict) and key in value:
                value = value[key]
            elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < len(value):
                value = value[int(key)]
            else:
                children = list(value) if isinstance(value, dict) else list(range(min(len(value), 20))) if isinstance(value, list) else []
                paths = [parent + "/" + str(child).replace("~", "~0").replace("/", "~1") for child in children[:20]]
                raise ValueError(f"JSON pointer {pointer!r} does not exist at {parent or '/'} "
                                 f"({type(value).__name__}). Available child pointers: {_encode(paths)[:1800]}. "
                                 "Request an existing path; if the needed fact is absent, identify a different source or measurement.")
            parent += "/" + token
        return value

    def _fragment(self, name: str, block: dict[str, Any], value: str, start: int, budget: int) -> tuple[str, dict[str, Any], tuple[Any, ...]]:
        if value == "" and start == 0:
            raise BufferError("Evidence window cannot fit an empty value and its location metadata.")
        if type(start) is not int or not 0 <= start < len(value):
            raise ValueError("Evidence character offset is outside the value.")
        field = "value" if "json_pointer" in block else "content"
        def fragment(end: int) -> dict[str, Any]:
            return {**block, field: value[start:end], "start_char": start, "end_char": end, "total_chars": len(value)}
        low, high = start, len(value)
        while low < high:
            end = (low + high + 1) // 2
            if len(_encode({name: fragment(end)})) <= budget:
                low = end
            else:
                high = end - 1
        if low == start:
            raise BufferError("Evidence window cannot fit a fragment and its location metadata.")
        result = fragment(low)
        return name, result, self.block_selector(name, result)

    def read(self, request: Any, budget: int, staged: list[tuple[Any, ...]] | None = None) -> tuple[str, dict[str, Any], tuple[Any, ...]] | None:
        selector = {"file": request} if isinstance(request, str) else request
        if not isinstance(selector, dict):
            raise ValueError("Evidence request must be a file path, JSON pointer or line range.")
        name = self.canonical(selector.get("file"))
        text = self.text(name)
        if "start_char" in selector and not any(field in selector for field in ("json_pointer", "start_line")):
            raise ValueError("start_char needs its original json_pointer or start_line; use pending_evidence.next_requests "
                             "or omit requested_files to continue. It is not a whole-file offset.")
        if not any(field in selector for field in ("json_pointer", "start_char", "start_line", "end_line")):
            try:
                if isinstance(json.loads(text), (dict, list)):
                    # Whole JSON evidence uses its structure regardless of pretty/compact
                    # formatting. Keep explicit line selections exact, not whole-file rereads.
                    selector = {**selector, "json_pointer": ""}
            except json.JSONDecodeError:
                pass
        if "json_pointer" in selector:
            pointer = selector["json_pointer"]
            if not isinstance(pointer, str) or pointer and not pointer.startswith("/"):
                raise ValueError("JSON pointer must be empty or start with '/'.")
            value = self.pointer_value(json.loads(text), pointer)
            key = (name, pointer)
            if any(previous_file == name and (previous_pointer == pointer or not previous_pointer
                   or pointer.startswith(previous_pointer + "/")) for previous_file, previous_pointer in
                   ([item for item in staged or [] if len(item) == 2])):
                return None
            block = {"json_pointer": pointer, "value": value}
            if isinstance(value, str) and ("start_char" in selector or len(_encode({name: block})) > budget):
                return self._fragment(name, {"json_pointer": pointer}, value, selector.get("start_char", 0), budget)
            if len(_encode({name: block})) > budget:
                # Small facts first: a long log/string must not hide sibling
                # controls required for a cross-file comparison. Pointers retain identity.
                keys = (sorted(value, key=lambda key: len(_encode(value[key]))) if isinstance(value, dict)
                        else range(len(value))) if isinstance(value, (dict, list)) else []
                children = [{**selector, "file": name,
                             "json_pointer": pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")}
                            for key in keys]
                raise BufferError("JSON value exceeds this read window; request a child pointer or relevant lines.", children)
            return name, block, key
        # Empty files still have readable zero-byte content. Whether an empty
        # asset is valid belongs to the review, not the read-selector protocol.
        lines = text.splitlines(keepends=True) or [""]
        start, end = selector.get("start_line", 1), selector.get("end_line", len(lines))
        if type(start) is not int or type(end) is not int or not 1 <= start <= len(lines) or end < start:
            raise ValueError(f"{name}: line range must be within 1..{len(lines)}.")
        end = min(end, len(lines))  # Reading the first N lines may reach EOF normally.
        requested_start, target_end = start, end
        ranges = [item[1:] for item in staged or [] if len(item) == 3 and item[0] == name]
        for low, high in sorted(ranges):
            if high < start:
                continue
            if low <= start:
                start = high + 1
            else:
                end = min(end, low - 1)
                break
        if start > end:
            return None
        if "start_char" in selector and start == requested_start:
            return self._fragment(name, {"start_line": start, "end_line": start,
                                  "remaining_lines": [start + 1, target_end] if start < target_end else []},
                                  lines[start - 1], selector["start_char"], budget)
        low, high = start - 1, end
        def content(stop: int) -> str:
            return _project_records("".join(lines[start - 1:stop]), start_line=start, line_numbers=True)

        if len(_encode({name: {"start_line": start, "end_line": end, "content": content(end), "remaining_lines": []}})) <= budget:
            low = high
        while low < high:
            stop = (low + high + 1) // 2
            block = {"start_line": start, "end_line": stop, "content": content(stop),
                     "remaining_lines": [stop + 1, target_end] if stop < target_end else []}
            if len(_encode({name: block})) <= budget:
                low = stop
            else:
                high = stop - 1
        if low < start:
            return self._fragment(name, {"start_line": start, "end_line": start,
                                  "remaining_lines": [start + 1, target_end] if start < target_end else []},
                                  lines[start - 1], 0, budget)
        return name, {"start_line": start, "end_line": low, "content": content(low),
                      "remaining_lines": [low + 1, target_end] if low < target_end else []}, (name, start, low)

    def window(self, requests: list[Any], budget: int, retained: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[Any], list[tuple[Any, ...]]]:
        """Prioritize requested sections, then retain recent context within budget.

        Historical reads may be needed for comparisons after window eviction;
        deduplicate this window, not against content absent from the model input.
        """
        pending, blocks, selectors = deepcopy(requests), {}, []
        while pending:
            origin = pending[0].get("_request", pending[0]) if isinstance(pending[0], dict) else pending[0]
            try:
                read = self.read(pending[0], budget - len(_encode(blocks)) - 256, selectors)
            except BufferError as exc:
                if len(exc.args) > 1 and exc.args[1]:
                    # Read exact children, never mark an omitted parent as read.
                    # The remaining requests stay queued across bounded windows.
                    pending[:1] = [{**child, "_request": origin} for child in exc.args[1]]
                    continue
                if blocks:
                    break
                raise
            request = pending.pop(0)
            if read is None:
                continue
            name, block, selector = read
            blocks.setdefault(name, []).append(block)
            selectors.append(selector)
            remaining = block.pop("remaining_lines", [])
            if "start_char" in block and block["end_char"] < block["total_chars"]:
                continuation = {**self.selector(selector), "start_char": block["end_char"], "_request": origin}
                if remaining:
                    continuation["end_line"] = remaining[1]
                pending.append(continuation)
            elif remaining:
                start, end = remaining
                pending.append({"file": name, "start_line": start, "end_line": end,
                                "_request": origin,
                                **({"condition": request["condition"]} if isinstance(request, dict) and request.get("condition") else {})})
        for name, prior in (retained or {}).items():
            for block in prior:
                if not self.covered(self.block_selector(name, block), selectors):
                    candidate = {**blocks, name: [*blocks.get(name, []), block]}
                    if len(_encode(candidate)) <= budget:
                        blocks = candidate
        return blocks, pending, selectors

    def queue_reads(self, pending: list[Any], requests: list[Any]) -> list[Any]:
        """Follow the current evidence question, retaining its unfinished reads.

        Keep the original selector with its continuations: a repeated whole-file
        request continues once; a narrowed/replaced question cancels obsolete reads.
        Empty requests continue the pending batch. Unsent content is never marked read.
        """
        def identity(request: Any) -> str:
            request = request.get("_request", request) if isinstance(request, dict) else request
            selector = {"file": request} if isinstance(request, str) else request
            if not isinstance(selector, dict):
                raise ValueError("Evidence request must be a file path, JSON pointer or line range.")
            fields = {key: selector[key] for key in ("json_pointer", "start_line", "end_line", "start_char") if key in selector}
            if fields.get("json_pointer") == "":
                fields.pop("json_pointer")
            return canonical_hash({"file": self.canonical(selector.get("file")), **fields})

        requests = [{key: value for key, value in request.items() if key != "_request"}
                    if isinstance(request, dict) else request for request in requests]
        if not requests:
            return deepcopy(pending)
        queued, seen = [], set()
        for request in requests:
            key = identity(request)
            if key not in seen:
                unfinished = [item for item in pending if identity(item) == key]
                queued.extend(deepcopy(unfinished or [request]))
                seen.add(key)
        return queued

    @staticmethod
    def selector(item: tuple[Any, ...]) -> dict[str, Any]:
        if len(item) in {3, 5}:
            return {"file": item[0], "start_line": item[1], "end_line": item[2],
                    **({"start_char": item[3]} if len(item) == 5 else {})}
        return {"file": item[0], "json_pointer": item[1],
                **({"start_char": item[2]} if len(item) == 4 else {})}


def _verdict_check(parsed: dict[str, Any], evidence: _Evidence, requirement_id: str,
                   pending_assets: dict[str, str], producer_ids: set[str]) -> dict[str, Any]:
    """Validate usable observations and repair instructions, not proof syntax."""
    status, summary = _status(parsed.get("status")), str(parsed.get("summary") or "").strip()
    if status not in {"pass", "fail", "needs_input", "needs_evidence"} or not summary:
        raise ValueError("Review needs status pass/fail/needs_input/needs_evidence and a short summary.")
    warnings = [text.strip() for text in parsed.get("warnings") or [] if isinstance(text, str) and text.strip()]
    issues, checks, observations, bindings = [], [], [], []
    for item in parsed.get("issues") or []:
        if not isinstance(item, dict):
            raise ValueError("Issue must be an object.")
        severity = str(item.get("severity") or "major").lower()
        if severity in {"minor", "warning", "info", "suggestion"}:
            warnings.append(str(item.get("message") or item.get("recommendation") or "").strip())
            continue
        message, action = str(item.get("message") or "").strip(), str(item.get("recommendation") or "").strip()
        if not message or not action:
            raise ValueError("Blocking issue needs a concrete message and executable recommendation.")
        file = item.get("file")
        structural = item.get("repair_kind") == "plan_structure"
        if structural and (not str(item.get("deliverable_id") or "").strip() or not isinstance(file, str) or not file.strip()):
            raise ValueError("Plan structure issue needs the missing delivery ID and path.")
        missing = item.get("deliverable_id") in pending_assets and file == pending_assets[item["deliverable_id"]]
        if not missing and not structural:
            file = evidence.canonical(file)
        issue = {**item, "file": file, "severity": "critical" if severity == "critical" else "major",
                 "code": str(item.get("code") or "authority_requirement_not_satisfied"),
                 "message": message, "recommendation": action, "repairable": True, "requirement_id": requirement_id}
        if structural or issue.get("step_id") not in producer_ids:
            issue.pop("step_id", None)
        if structural or issue.get("source_file") not in evidence.hashes:
            issue.pop("source_file", None)
        elif issue.get("source_file"):
            issue["source_file"] = evidence.canonical(issue["source_file"])
        issues.append(issue)
    # A usable defect must reach the producer even if optional passing-check
    # metadata is malformed. Only a pass needs a complete, valid checklist.
    if issues:
        status = "fail"
    elif status == "needs_evidence":
        return {"status": status}
    for item in parsed.get("condition_checks") or []:
        if not isinstance(item, dict):
            if status == "fail":
                continue
            raise ValueError("Condition check must be an object.")
        condition, observation = str(item.get("condition") or "").strip(), str(item.get("observation") or "").strip()
        outcome = _status(item.get("status"))
        names = item.get("files") or []
        if not condition or not observation or not isinstance(names, list) or not names:
            if status == "fail":
                continue
            raise ValueError("Condition check needs condition, files and a concise observation.")
        if outcome not in {"pass", "fail", "needs_evidence"}:
            if status == "fail":
                continue
            raise ValueError(f"Condition {condition!r} status must be pass/fail/needs_evidence, got {outcome!r}.")
        unknown = [name for name in names if not isinstance(name, str) or name not in evidence.hashes]
        if unknown:
            if status == "fail":
                continue
            children = [path for path in evidence.hashes if any(
                isinstance(name, str) and path.startswith(name.rstrip("/") + "/") for name in unknown)][:10]
            raise ValueError(f"Condition {condition!r} cites unknown inventory files: {_encode(unknown)[:800]}. "
                             f"Cite exact inventory files, not directories. Existing children: {_encode(children)[:1800]}.")
        entries = [{"file": evidence.canonical(name), "sha256": evidence.hashes[evidence.canonical(name)],
                    "observation": observation} for name in names]
        indices = []
        for entry in entries:
            if entry not in observations:
                observations.append(entry)
            indices.append(observations.index(entry))
        checks.append({"condition_id": item.get("condition_id") or canonical_hash(condition)[:16], "condition": condition,
                       "status": outcome, "observation": observation, "evidence_indices": indices})
    for item in parsed.get("asset_bindings") or []:
        if not isinstance(item, dict) or not isinstance(item.get("asset_id"), str):
            if status == "fail":
                continue
            raise ValueError("Asset binding needs an asset_id and inventory paths.")
        paths = item.get("paths") or []
        if not isinstance(paths, list) or not paths or any(path not in evidence.delivered for path in paths):
            if status == "fail":
                continue
            raise ValueError("Asset binding must use actual delivery paths.")
        bindings.append({"asset_id": item["asset_id"], "paths": list(dict.fromkeys(paths))})
    if status == "fail" and not issues and warnings and checks and all(item["status"] == "pass" for item in checks):
        status = "pass"
    if status == "fail" and not issues:
        raise ValueError("Fail needs a blocking defect with its actual file and recommendation.")
    if status == "pass":
        unresolved = [item["condition"] for item in checks if item["status"] != "pass"]
        if not checks or unresolved:
            raise ValueError("Pass must resolve the open checks using their condition_id from previous_review: "
                             + _encode(unresolved) + ". Update those checks from evidence, or return the actual defect.")
        if pending_assets.keys() - {item["asset_id"] for item in bindings}:
            raise ValueError("Bind equivalent existing assets to pending_asset_bindings, or report a real missing-content defect.")
    if not observations:
        observations = [{"file": item["file"], "sha256": evidence.hashes.get(item["file"], ""),
                         "observation": item["message"], "exists": item["file"] in evidence.hashes} for item in issues]
    result = {
        "review_type": "model_authority_alignment", "status": status, "summary": summary,
        "delivery_kind": "dataset_materialized", "warnings": list(dict.fromkeys(filter(None, warnings))),
        "requirement_checks": [{"requirement_id": requirement_id, "status": status, "reason": summary,
                                "delivery_kind": "dataset_materialized", "condition_checks": checks,
                                "evidence": observations}],
        "issues": issues, "asset_bindings": bindings,
    }
    if status == "fail":
        result["revision_contract"] = revision_contract(issues)
    if status == "needs_input":
        result.update(needs_input_result(question=str(parsed.get("question") or summary), context=summary,
                                         options=parsed.get("options") or []))
    return result


async def review_delivery_against_authority(
    state: State, *, files: dict[str, str], preprocessing_request: dict[str, Any],
    work_order: dict[str, Any], plan_context: dict[str, Any], deterministic_review: dict[str, Any],
    package_dir: str | Path | None = None, producer_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Review once, read missing evidence on demand, repair only genuine defects."""
    client = state.hook_state.get("_data_planning_llm_client")
    if client is None or not callable(getattr(client, "chat", None)):
        return _reviewer_error("authority_reviewer_unavailable", "Semantic request alignment could not be verified: reviewer unavailable.")
    retry_budget = getattr(client, "retry_budget_s", None)
    client = client.background_copy() if callable(getattr(client, "background_copy", None)) else copy(client)
    client = data_model_client(client)
    try:
        max_input = int(os.getenv("DATA_AUTHORITY_REVIEW_MAX_INPUT_CHARS", "64000"))
        max_output = int(os.getenv("DATA_AUTHORITY_REVIEW_MAX_OUTPUT_TOKENS", "8192"))
        request_timeout = float(os.getenv("DATA_AUTHORITY_REVIEW_REQUEST_TIMEOUT_S", "60"))
        max_verifications = int(os.getenv("DATA_AUTHORITY_REVIEW_MAX_VERIFICATIONS", "4"))
        max_calls = int(os.getenv("DATA_AUTHORITY_REVIEW_MAX_CALLS", "12"))
        max_seconds = float(os.getenv("DATA_AUTHORITY_REVIEW_MAX_SECONDS", "600"))
        if (max_input <= 0 or max_output < 1024 or max_verifications <= 0 or max_calls <= 0
                or not math.isfinite(max_seconds) or max_seconds <= 0
                or not math.isfinite(request_timeout) or request_timeout <= 0):
            raise ValueError("Review budgets must be positive/finite and output budget at least 1024.")
        evidence = _Evidence(files, producer_evidence or {}, package_dir)
    except (ValueError, OSError) as exc:
        return _reviewer_error("authority_review_configuration_or_evidence", str(exc))
    reasoning = reasoning_options(client, os.getenv("DATA_AUTHORITY_REVIEW_REASONING_EFFORT", "low").strip()).get("reasoning_effort")
    requirements = _requirement_list(preprocessing_request, work_order, plan_context)
    requirement_id = requirements[0]["requirement_id"]
    proposed = {item["id"] for item in plan_context.get("required_deliverables") or []
                if isinstance(item, dict) and item.get("id") and item.get("output_path_is_explicit") is False}
    pending = {issue["deliverable_id"]: str(issue.get("file") or "") for issue in deterministic_review.get("issues") or []
               if issue.get("code") == "required_asset_unfulfilled" and issue.get("deliverable_id") in proposed}
    producer_map = [{
        "step_id": item["step_id"], "tool_name": item.get("tool_name"),
        "dependencies": item.get("dependencies") or [],
        # Full tool contracts belong to the repair call, not every review turn.
        "repair_field": item.get("repair_field"),
        "report_rebuild": item.get("report_rebuild") or {},
        "derived_report_files": [evidence.canonical(name) for name in item.get("derived_report_files") or []
                                 if name in evidence.hashes],
        "native_reviews": item.get("native_reviews") or {},
        "native_measurements": item.get("native_measurements") or {},
        "executed_parameters": item.get("executed_parameters") or {},
        "native_execution": item.get("native_execution") or {},
        "files": list(dict.fromkeys(evidence.canonical(name) for name in item.get("staged_files") or []
                                   if name in evidence.hashes)),
        "source_files": list(dict.fromkeys(evidence.canonical(name) for name in item.get("materialized_files") or []
                                          if name in evidence.hashes and evidence.canonical(name) not in {
                                              evidence.canonical(path) for path in item.get("staged_files") or []
                                              if path in evidence.hashes})),
    } for item in plan_context.get("producer_map") or [] if isinstance(item, dict) and item.get("step_id")]
    producer_ids = {item["step_id"] for item in producer_map}
    asset_fingerprints = dict(evidence.hashes)
    views = evidence.overview()
    # Producer receipts are readable evidence, not unbounded locked authority.
    # Keep the DAG inline; page large fields through the existing evidence reader.
    for producer in producer_map:
        measurements = producer["native_measurements"]
        source_geometry = measurements.get("source_geometry") or {}
        source_path = source_geometry.get("path")
        if source_path in evidence.hashes and source_geometry.get("sha256") == evidence.hashes[source_path]:
            producer["measured_source_geometry"] = source_geometry
        source = measurements.get("measurement_source") or {}
        path = source.get("path")
        if path in evidence.hashes and source.get("sha256") == evidence.hashes[path]:
            delivered = evidence.canonical(path)
            if delivered in evidence.delivered:
                producer["measured_delivery"] = {
                    "path": delivered, "sha256": source["sha256"], "method": source.get("method"), "format": source.get("format"),
                }
                producer["measured_observations"] = {key: measurements[key] for key in (
                    "bounds_min", "bounds_max", "n_nodes", "n_2d_elements", "n_3d_elements",
                    "duplicate_nodes", "duplicate_elements", "isolated_nodes", "physical_group_geometry",
                    "surface_region_measurements", "boundary_clearance", "tetrahedral_measurements", "mesh_sizing_semantics",
                ) if key in measurements}
                if len(_encode(producer["measured_observations"])) > 4096:
                    producer.pop("measured_observations")  # Complete measurements remain in the paged evidence.
        mesh_check = measurements.get("openfoam_check_mesh") or {}
        sources = mesh_check.get("measurement_sources") or {}
        if sources and all(path in evidence.hashes and evidence.hashes[path] == digest
                           and evidence.canonical(path) in evidence.delivered for path, digest in sources.items()):
            producer["measured_openfoam"] = {
                "files": [evidence.canonical(path) for path in sources],
                **{key: mesh_check[key] for key in ("scope", "status", "observations") if key in mesh_check},
            }
        for field in ("native_reviews", "native_measurements", "executed_parameters", "native_execution"):
            value = producer[field]
            if len(_encode(value)) > 1024:
                name = f"producer:{producer['step_id']}:{field}"
                views[name] = evidence.add_verification(name, {field: value}, role="producer")
                producer[field] = {"evidence_file": name, "sha256": evidence.hashes[name]}
    # Producer measurements are already summarized above. Full receipts stay
    # readable on demand; duplicating them here crowds out the delivered files.
    native_views = set()
    analysis = plan_context.get("requirement_analysis") or {}
    software = plan_context.get("simulation_software") or analysis.get("target_software") or {}
    # Read final solver bytes with a reusable parser. Model-written verifiers
    # used to confuse NODE OUTPUT/ELEMENT OUTPUT, NSET vs ELSET and qualified
    # instance IDs, spending the verification budget on their own parser bugs.
    native_inspections = []
    rechecked_native_codes = set()
    netcdf_inspections = {}
    for path in sorted(evidence.delivered):
        if Path(path).suffix.lower() not in {".nc", ".nc4", ".cdf"}:
            continue
        from .netcdf_inspection import inspect_netcdf

        try:
            measured = inspect_netcdf(evidence.paths[path])
        except ImportError as exc:
            return {**_reviewer_error("native_netcdf_unavailable", f"Install the Data node's declared netCDF4 dependency: {exc}"),
                    "failure_category": "environment_required", "required_dependencies": ["netCDF4"]}
        except (OSError, ValueError) as exc:
            measured = {"status": "fail", "issues": [{"code": "netcdf_readback_failed",
                "message": f"Native NetCDF read-back failed: {exc}",
                "recommendation": "Repair the invalid NetCDF output from the authoritative inputs; preserve unrelated files."}]}
        name = "native_inspection:" + path
        views[name] = evidence.add_verification(name, measured, role="native_measurements")
        native_views.add(name)
        native_inspections.append({"file": path, "sha256": evidence.hashes[path], "evidence_file": name})
        netcdf_inspections[path] = measured
        if measured["issues"]:
            return {"status": "fail", "summary": "Delivered NetCDF contradicts native CF metadata checks.",
                    "issues": [{**item, "file": path, "severity": "major", "repairable": True,
                                "requirement_id": requirement_id} for item in measured["issues"]],
                    "observations": [], "condition_checks": [], "native_inspection": measured}
    # Compare actual producer/consumer files with the installed reader. Do not
    # ask the model to invent a new numerical validator on each review turn.
    from .netcdf_inspection import measure_conservation

    by_step = {item["step_id"]: item for item in producer_map}
    comparisons = set()
    for producer in producer_map:
        sources = {evidence.canonical(path) for dependency in producer.get("dependencies") or []
                   for path in (by_step.get(dependency) or {}).get("files") or []
                   if path in evidence.hashes}
        for target in sorted(set(producer.get("files") or []) & netcdf_inspections.keys()):
            for source in sorted(sources & netcdf_inspections.keys() - {target}):
                shared = netcdf_inspections[source].get("variables", {}).keys() & netcdf_inspections[target].get("variables", {}).keys()
                for variable in sorted(shared):
                    if len(netcdf_inspections[target]["variables"][variable].get("dimensions", [])) < 2:
                        continue
                    key = (source, target, variable)
                    if key in comparisons:
                        continue
                    comparisons.add(key)
                    try:
                        measured = measure_conservation(evidence.paths[source], evidence.paths[target], variable)
                    except (ValueError, KeyError, OSError):
                        continue  # This reader does not establish other transformations.
                    name = "native_comparison:" + canonical_hash(key)[:24]
                    views[name] = evidence.add_verification(name, {"source_file": source, "target_file": target,
                        "variable": variable, "inputs": {path: evidence.hashes[path] for path in (source, target)}, **measured}, role="native_measurements")
                    native_views.add(name)
                    native_inspections.append({"evidence_file": name})
    if (isinstance(software, dict) and "wrf" in str(software.get("name") or "").lower()
            and any(Path(name).name.lower() == "namelist.input" for name in evidence.delivered)):
        from .namelist_inspection import inspect_wrf_files

        try:
            measured = await inspect_wrf_files(state, {name: evidence.text(name) for name in evidence.delivered
                if Path(name).name.lower() in {"namelist.input", "namelist.wps"}}, software)
        except ImportError as exc:
            return {**_reviewer_error("native_namelist_unavailable", f"Install the Data node's declared f90nml dependency: {exc}"),
                    "failure_category": "environment_required", "required_dependencies": ["f90nml"]}
        except (ValueError, OSError, asyncio.TimeoutError) as exc:
            return _reviewer_error("native_schema_unavailable", f"WRF native schema inspection could not complete: {exc}")
        schema_issues = [{**issue, "file": issue.get("file", item["file"]), "severity": "major", "repairable": True,
                          "requirement_id": requirement_id}
                         for item in measured["files"] for issue in item["issues"]]
        name = "native_inspection:wrf_registry"
        views[name] = evidence.add_verification(name, measured, role="native_measurements")
        native_views.add(name)
        native_inspections.append({"evidence_file": name})
        if schema_issues:
            state.append_transcript("native_schema_findings", reference=measured["reference_url"], issues=schema_issues)
            return {"status": "fail", "summary": "WRF namelist contradicts its native Registry or WPS dates.",
                    "issues": schema_issues, "reference": measured, "condition_checks": [], "observations": []}
        if measured["files"]:
            rechecked_native_codes.update({"wrf_namelist_schema", "wrf_namelist_parse"})
    from .namelist_inspection import mpas_atmosphere_schema, inspect_mpas_configuration

    try:
        schema = await mpas_atmosphere_schema(state, software, evidence.delivered)
        schema_issues = []
        for path in sorted(evidence.delivered) if schema else []:
            if Path(path).name not in {"namelist.atmosphere", "streams.atmosphere"}:
                continue
            measured = inspect_mpas_configuration(evidence.text(path), path, schema)
            name = "native_inspection:" + path
            views[name] = evidence.add_verification(name, measured, role="native_measurements")
            native_views.add(name)
            native_inspections.append({"file": path, "sha256": evidence.hashes[path], "evidence_file": name,
                "status": measured["status"], "scope": measured["scope"],
                **({"observations": measured["observations"]} if len(_encode(measured["observations"])) <= 4096 else {})})
            schema_issues.extend({**item, "severity": "major", "repairable": True, "requirement_id": requirement_id}
                                 for item in measured["issues"])
        if schema_issues:
            return {"status": "fail", "summary": "Configuration contradicts the versioned MPAS Registry.",
                "issues": schema_issues, "reference": {key: schema[key] for key in
                    ("reference_url", "reference_version", "reference_sha256", "scope")},
                "condition_checks": [], "observations": []}
        if schema:
            rechecked_native_codes.add("mpas_configuration_schema")
    except ImportError as exc:
        return {**_reviewer_error("native_namelist_unavailable", f"The declared f90nml dependency is unavailable: {exc}"),
                "failure_category": "environment_required", "required_dependencies": ["f90nml"]}
    except (ValueError, OSError, TimeoutError) as exc:
        return _reviewer_error("native_schema_unavailable", f"MPAS schema inspection could not complete: {exc}")
    inp_files = sorted(name for name in evidence.delivered if name.lower().endswith(".inp") and name not in evidence.binary)
    geometries = [item["parameter_bindings"]["geometry"] for item in analysis.get("required_files") or []
                  if isinstance(item, dict) and isinstance(item.get("parameter_bindings"), dict)
                  and isinstance(item["parameter_bindings"].get("geometry"), dict)]
    for path in inp_files[:4]:
        if next(item["size_bytes"] for item in evidence.inventory if item["path"] == path) > 16*1024*1024:
            continue
        from .abaqus_inspection import inspect_planar_inp

        text = evidence.text(path)
        if not all(re.search(r"(?im)^\s*\*" + keyword + r"\s*(?:,|$)", text) for keyword in ("NODE", "ELEMENT")):
            continue  # .inp is also used by unrelated solvers such as CP2K.
        measurement = inspect_planar_inp(text, geometries[0] if len(inp_files) == len(geometries) == 1 else None,
                                        solver=str(software))
        element_format = measurement.get("element_format") or {}
        if element_format.get("unsupported_types"):
            return {"status": "fail", "summary": "Element names are not accepted by the target solver.",
                    "issues": [{"file": path, "code": "solver_element_type", "severity": "error",
                        "message": "CalculiX does not accept these planar element names: " + ", ".join(element_format["unsupported_types"]),
                        "recommendation": "Use the target solver's supported formulation with the required order and topology; preserve mesh connectivity and frozen physics.",
                        "diagnostic_context": element_format}], "condition_checks": [], "observations": []}
        if measurement.get("undefined_surface_load_references"):
            # Return the native defect to its producer immediately instead of
            # spending the review session rediscovering a keyword reference rule.
            return {"status": "fail", "summary": "Abaqus DSLOAD references surfaces but none are defined.",
                    "issues": [{"file": path, "code": "abaqus_surface_reference", "severity": "error",
                        "message": "DSLOAD requires a SURFACE, not NSET/ELSET: "
                                   + ", ".join(measurement["undefined_surface_load_references"]),
                        "recommendation": "Define the loaded surface and reference it from DSLOAD, preserving the requested load.",
                        "diagnostic_context": {"source_sha256": evidence.hashes[path], "observations": measurement,
                            "reference": "https://docs.software.vt.edu/abaqusv2025/English/SIMACAEKEYRefMap/simakey-r-dsload.htm"}}],
                    "condition_checks": [], "observations": []}
        name = "native_inspection:" + path
        receipt = {"source_file": path, "source_sha256": evidence.hashes[path],
                   "method": "Data native single-part planar INP reader", "observations": measurement}
        views[name] = evidence.add_verification(name, receipt, role="native_measurements")
        native_views.add(name)
        native_inspections.append({"file": path, "sha256": evidence.hashes[path], "evidence_file": name,
            **({"element_format": element_format} if element_format else {}),
            **({"observations": measurement} if len(_encode(measurement)) <= 4096 else {})})
    native_checks = deterministic_review.get("common_checks") or {}
    finding_fields = ("file", "code", "message", "recommendation", "severity", "step_id", "source_file", "deliverable_id", "repair_kind")
    incoming_findings = [{key: item[key] for key in finding_fields if key in item}
                         for item in plan_context.get("review_feedback") or [] if isinstance(item, dict)
                         and item.get("code") not in rechecked_native_codes]
    inputs = preprocessing_request.get("inputs") or []
    dispatch_findings = [
        {key: item[key] for key in ("file", "code", "message", "recommendation", "step_id", "source_file") if key in item}
        for context in (inputs if isinstance(inputs, list) else [inputs]) if isinstance(context, dict)
        for item in context.get("review_feedback") or [] if isinstance(item, dict)
        and item.get("code") not in rechecked_native_codes
    ]
    # Tool identity selects format semantics. Analyst provenance/dispatch notes
    # are not caller requirements and must not become a second authority here.
    if isinstance(software, dict):
        software = {key: software[key] for key in ("name", "version") if key in software}
    payload = {
        "authority_requirements": requirements,
        "target_software": software,
        "delivery_plan": [{key: item.get(key) for key in
                           ("id", "declared_output_path", "format", "delivery_required", "output_path_is_explicit")}
                          for item in plan_context.get("required_deliverables") or [] if isinstance(item, dict)],
        "producer_map": producer_map, "delivery_inventory": evidence.inventory,
        "native_file_inspections": native_inspections,
        "production_steps": [{key: item.get(key) for key in ("id", "tool_name", "inputs", "outputs", "dependencies")}
                             for item in plan_context.get("generation_steps") or []],
        "delivery_destination": plan_context.get("delivery_destination"),
        "pending_asset_bindings": sorted(pending), "previous_findings": incoming_findings,
        "dispatch_findings": dispatch_findings,
        "dispatch_diagnostics_unverified": {key: value for key, value in (state.hook_state.get("node_inputs") or {}).items()
            if key in {"review_feedback", "recovery_feedback"}},
        "unresolved_facts": analysis.get("unresolved_facts") or [],
        "quality_references": analysis.get("reference_evidence") or [],
        "inspected_input_sources": [{**{key: item[key] for key in ("path", "requested_path", "size_bytes")
                                                        if item.get(key) is not None},
                                     "sha256": evidence.hashes.get(item.get("path")) or item.get("sha256")}
                                   for item in analysis.get("local_asset_inventory") or [] if isinstance(item, dict)],
        "native_validation": {
            "scope": "Only the named integrity/format/measurement checks below; no implicit target-software key or reference validation.",
            "status": "pending_asset_bindings" if pending else deterministic_review.get("status"),
            "passed_integrity_checks": sum(value is True for value in native_checks.values()),
            "passed_checks": [key for key, value in native_checks.items() if value is True],
            "checks": {key: value for key, value in native_checks.items()
                       if value is not True and key not in {f"required_asset:{item}" for item in pending}},
            "issues": [issue for issue in deterministic_review.get("issues") or []
                       if issue.get("deliverable_id") not in pending],
        },
        "verification_runtime": {"input_files": "dict[str,str]: exact inventory keys -> read-only filesystem paths; not file contents, not a module",
            "text_read_example": "from pathlib import Path\ntext = Path(input_files[" + json.dumps(next(iter(files), "file")) + "]).read_text()",
            "native_readers": {
                "Gmsh MSH2/MSH4": "from nodes.data.tools.mesh_generator import read_gmsh_mesh_quality; "
                    "q = read_gmsh_mesh_quality(input_files[mesh_key]). Returns counts, bounds, duplicate_nodes, "
                    "duplicate_elements, isolated_nodes, tetrahedral_measurements and SHA-256 binding. "
                    "Use this installed reader for comparisons; do not implement MSH block parsing.",
                "OpenFOAM ASCII 2D": "from nodes.data.tools.mesh_generator import extract_2d_domain_loops_from_polymesh; "
                    "read the parent of input_files['constant/polyMesh/points']; select points, faces and boundary. "
                    "Returns outer_boundary_points, body_loops and profile_points in physical XY coordinates. "
                    "Use this reader instead of rewriting the format parser.",
            },
            "cwd": "temporary scratch; selected relative inventory paths are also bound read-only; no filesystem discovery needed"},
    }
    if len(_encode(payload["native_validation"])) > 2048:
        name = "validation:package"
        views[name] = evidence.add_verification(name, payload["native_validation"], role="producer")
        payload["native_validation"] = {key: payload["native_validation"][key] for key in
                                        ("scope", "status", "passed_integrity_checks")}
        payload["native_validation"].update(evidence_file=name, sha256=evidence.hashes[name])
    # Auxiliary planning context is evidence, not locked caller authority.
    # Directory inventories/reference lists must not displace the actual files
    # throughout every comparison. Preserve complete values in the same reader.
    for field in ("inspected_input_sources", "quality_references", "unresolved_facts", "dispatch_diagnostics_unverified"):
        if len(_encode(payload[field])) > 1024:
            name = f"context:{field}"
            views[name] = evidence.add_verification(name, payload[field], role="context")
            payload[field] = {"evidence_file": name, "sha256": evidence.hashes[name]}
    # Reuse a complete verdict only for exactly unchanged authority, context AND
    # all actual bytes; do not reconstruct per-condition dependency closures.
    authority = canonical_hash({"requirements": requirements, "request": {
        key: preprocessing_request.get(key) for key in ("request_id", "request_spec_hash", "review_profile")}})
    context_fingerprints = {key: canonical_hash(value) for key, value in payload.items()}
    scope = canonical_hash({"payload": payload, "authority": authority,
        "native_checks": native_checks, "contract": [_SYSTEM_PROMPT, _REVIEW_TOOL],
        "verification_implementation": inspect.getsource(_verify_requested_evidence) + inspect.getsource(_reference_excerpt) + inspect.getsource(_cache_reference)
                                       + inspect.getsource(_completed_observations) + inspect.getsource(_Evidence)
                                       + inspect.getsource(_verdict_check) + inspect.getsource(review_delivery_against_authority),
        "model": getattr(client, "model", ""), "endpoint": getattr(client, "base_url", "")})
    previous = state.hook_state.get("_data_authority_review") or {}
    # Verdict reuse requires identical context, but redispatch/context changes
    # cannot buy another budget for the same caller and delivered bytes. Source
    # evidence may change on resume without constituting a repaired delivery.
    delivery_fingerprints = {name: asset_fingerprints[name] for name in sorted(evidence.delivered)}
    previous_delivery = previous.get("delivery_fingerprints")
    if previous_delivery is None and previous.get("fingerprints"):
        previous_delivery = {name: previous["fingerprints"].get(name) for name in delivery_fingerprints}
    same_session = previous.get("authority") == authority and previous_delivery == delivery_fingerprints
    references: dict[str, Any] = {}
    if previous.get("authority") == authority:
        # Source retrieval is independent of an artifact verdict. Reuse the
        # verified bytes after repair/protocol recovery, but rerun checks on
        # the changed artifacts; never carry an old check's PASS forward.
        candidates = [*(previous.get("reference_sources") or {}).values(),
                      *((previous.get("progress") or {}).get("verifications") or {}).values()]
        for receipt in candidates:
            reference = receipt.get("reference_file") or {}
            if not reference or not receipt.get("url"):
                continue
            try:
                evidence.add_reference(reference)
            except (OSError, ValueError):
                continue  # A missing/changed cache is reacquired, not an artifact defect.
            references[reference["name"]] = {key: receipt[key] for key in
                ("url", "source_sha256", "reference_file") if key in receipt}
            views[reference["name"]] = evidence.overview({reference["name"]}).get(reference["name"], "")
    if previous.get("scope") == scope and previous.get("fingerprints") == asset_fingerprints and previous.get("result"):
        result = deepcopy(previous["result"])
        result["review_attempts"] = 0
        emit_progress(state, "authority_review_done", status=result["status"], calls=0, reused=True)
        return result
    if previous.get("authority") == authority and previous.get("result", {}).get("status") == "fail" and not incoming_findings:
        payload["previous_findings"] = [{key: item[key] for key in finding_fields if key in item}
                                        for item in previous["result"].get("issues") or []]

    overhead = len(_SYSTEM_PROMPT) + len(_encode([_REVIEW_TOOL]))
    working: dict[str, Any] = {}
    draft: dict[str, Any] = {}
    corrections, calls, error = 0, 0, ""
    elapsed = 0.0
    pending_reads: list[Any] = []
    exhausted_request = ""
    verifications: dict[str, Any] = {}
    config = {"max_input": max_input, "max_output": max_output, "reasoning": reasoning,
              "max_verifications": max_verifications, "max_calls": max_calls, "max_seconds": max_seconds,
              "request_timeout_s": request_timeout, "model": getattr(client, "model", ""),
              "provider": canonical_hash(str(getattr(client, "base_url", "")))}
    resuming = previous.get("scope") == scope and previous.get("fingerprints") == asset_fingerprints
    if same_session and not resuming:
        progress = previous.get("progress") or {}
        costs = previous.get("result") or {}
        calls = progress.get("calls", costs.get("review_attempts", 0))
        elapsed = progress.get("elapsed_s", costs.get("review_elapsed_s", 0.0))
        emit_progress(state, "authority_review_context_changed", terminal=False,
                      preserved_calls=calls, preserved_elapsed_s=round(elapsed, 3),
                      scope_changed=previous.get("scope") != scope,
                      evidence_changed=previous.get("fingerprints") != asset_fingerprints,
                      changed_context_fields=[key for key in sorted(context_fingerprints)
                          if previous.get("context_fingerprints", {}).get(key) != context_fingerprints[key]]
                          if previous.get("context_fingerprints") else ["legacy_context_not_recorded"])
    if resuming:
        progress = deepcopy(previous.get("progress") or {})
        working, draft = progress.get("working") or {}, _completed_observations(progress.get("draft") or {})
        pending_reads = progress.get("pending_reads") or []
        error, corrections = progress.get("error", ""), progress.get("corrections", 0)
        exhausted_request = previous.get("exhausted_request") or ""
        verifications = progress.get("verifications") or {}
        calls, elapsed = progress.get("calls", 0), progress.get("elapsed_s", 0.0)
        if all(previous.get("config", {}).get(key) == config[key] for key in ("reasoning", "model", "provider")):
            reasoning = reasoning_options(client, progress.get("reasoning_effort", reasoning)).get("reasoning_effort")
        if (exhausted_request and "Output budget exhausted" in error and reasoning != "none"
                and reasoning_options(client, "none")):
            reasoning, exhausted_request = "none", ""
        # Changing a budget can permit continuation, but must not erase the
        # evidence/cost of this same session or repeat successful work.
        if previous.get("config") != config:
            exhausted_request = ""
        for name, receipt in verifications.items():
            if receipt.get("reference_file"):
                evidence.add_reference(receipt["reference_file"])
            views[name] = evidence.add_verification(name, receipt)

    # Evidence is reusable independently of the verdict. A local asset repair
    # invalidates observations, not unrelated measurements or a previously
    # executed method. Changed dependencies require a fresh verification request;
    # never replay old model-written scripts automatically.
    if not resuming and previous.get("authority") == authority:
        prior_receipts = {**((previous.get("progress") or {}).get("verifications") or {}),
                          **((previous.get("result") or {}).get("verification_evidence") or {})}
        for name, receipt in prior_receipts.items():
            if name in verifications:
                continue
            inputs = receipt.get("inputs") or {}
            if ((receipt.get("execution_status") != "success" and not same_session)
                    or (not inputs and receipt.get("tool") not in {"web_search", "web_fetch", "read_file"})
                    or any(key not in evidence.hashes for key in inputs)):
                continue
            if all(evidence.hashes[key] == digest for key, digest in inputs.items()):
                if not same_session:
                    receipt = {**receipt, "reused_evidence": True}
                verifications[name] = receipt
                views[name] = evidence.add_verification(name, receipt)
    initial_calls = calls

    def checkpoint() -> None:
        state.hook_state["_data_authority_review"] = {
            "scope": scope, "authority": authority, "fingerprints": asset_fingerprints, "config": config,
            "delivery_fingerprints": delivery_fingerprints,
            "context_fingerprints": context_fingerprints,
            "reference_sources": deepcopy(references),
            **({"exhausted_request": exhausted_request} if exhausted_request else {}),
            "progress": deepcopy({"working": working, "draft": draft, "pending_reads": pending_reads,
                "error": error, "corrections": corrections, "verifications": verifications,
                "calls": calls, "elapsed_s": elapsed, "reasoning_effort": reasoning})}

    def session_error(code: str, message: str) -> dict[str, Any]:
        checkpoint()
        return {**_reviewer_error(code, message), "review_attempts": calls,
                "review_session": {"calls": calls, "elapsed_s": round(elapsed, 3),
                    "max_calls": max_calls, "max_seconds": max_seconds,
                    "pending_reads": pending_reads,
                    "last_evidence_request": draft.get("last_evidence_request"),
                    "unresolved_checks": [item for item in draft.get("condition_checks") or []
                                          if _status(item.get("status")) != "pass"]}}

    def verification_budget() -> dict[str, int]:
        receipts = [item for item in verifications.values()
                    if not item.get("readback_only") and not item.get("reused_evidence")]
        # Reference retrieval is bounded by attempts/time; it must not consume
        # the successful numerical-measurement allowance. Reusing evidence
        # after a real asset repair does not execute it again or incur new cost.
        return {
            "verifications": max(0, max_verifications - sum(
                item.get("execution_status") == "success" and item.get("tool") in
                {"execute_python", "measure_mesh_regions", "measure_netcdf_conservation"} for item in receipts)),
            "verification_attempts": max(0, max_verifications * 2 - len(receipts)),
        }

    async def acquire(request: dict[str, Any]) -> str | None:
        nonlocal elapsed
        if not isinstance(request, dict):
            raise ValueError("Verification request must be an object.")
        fields = {"web_search": ("query",), "web_fetch": ("url", "terms", "offset", "limit"),
                  "read_file": ("path", "terms", "offset", "limit"), "execute_python": ("code",),
                  "measure_mesh_regions": ("regions",),
                  "measure_netcdf_conservation": ("source_file", "target_file", "variable")}
        operation = {key: request[key] for key in fields.get(request.get("tool"), ()) if key in request}
        if request.get("tool") in {"web_fetch", "read_file"}:
            operation.update(offset=request.get("offset", 0), limit=min(int(request.get("limit", 80)), 120))
        name = "verification:" + canonical_hash({"tool": request.get("tool"),
            "files": sorted(request.get("files") or []), **operation})[:24]
        if name in verifications:
            return name
        inventory_read = request.get("tool") == "read_file" and (
            request.get("path") in evidence.hashes or any(str(path) == request.get("path") for path in evidence.paths.values()))
        if not inventory_read and not all(verification_budget().values()):
            raise ValueError("Verification budget exhausted. Use the existing native observations and verification "
                             "receipts via requested_files; do not compute them again. If they cannot resolve an "
                             "essential condition, identify that missing measurement without claiming pass.")
        if elapsed >= max_seconds:
            raise TimeoutError("Review session budget exhausted before verification.")
        started = time.monotonic()
        try:
            async with asyncio.timeout(min(90, max_seconds - elapsed)):
                receipt = await _verify_requested_evidence(state, request, evidence)
        except ValueError:
            raise
        except Exception as exc:
            receipt = {"tool": request.get("tool"), "condition": request.get("condition"),
                       "scope": request.get("scope"), "execution_status": "unavailable",
                       "inputs": {key: evidence.hashes[key] for key in request.get("files", []) if key in evidence.hashes},
                       **({"url": request["url"]} if request.get("url") else {}),
                       "error": f"{type(exc).__name__}: {exc}"}
        finally:
            elapsed += time.monotonic() - started
            checkpoint()
        verifications[name] = receipt
        if receipt.get("reference_file") and receipt.get("url"):
            references[receipt["reference_file"]["name"]] = {key: receipt[key] for key in
                ("url", "source_sha256", "reference_file") if key in receipt}
        views[name] = evidence.add_verification(name, receipt)
        checkpoint()
        return name

    def assemble() -> dict[str, Any]:
        nonlocal working
        content = {**payload, "evidence_read_budget_chars": 0, "evidence": working,
                   "remaining_review_budget": {"model_calls": max_calls - calls,
                                               "active_seconds": round(max_seconds - elapsed, 1),
                                               **verification_budget()},
                   **({"pending_evidence": {"sections": len(pending_reads), "files": list(dict.fromkeys(
                       item if isinstance(item, str) else item.get("file") for item in pending_reads)),
                       "next_requests": [{key: value for key, value in item.items() if key != "_request"}
                                         if isinstance(item, dict) else item for item in pending_reads[:2]]}} if pending_reads else {}),
                   # Request selectors already live in the reader queue; do not
                   # repeat them (and verification scopes) in every observation.
                   **({"previous_review": {key: value for key, value in draft.items()
                          if key != "last_evidence_request"},
                       "last_evidence_request": (draft.get("last_evidence_request") or {}).get("question", "")}
                      if draft else {}),
                   **({"reference_sources": list(references.values())} if references else {}),
                   **({"protocol_correction": error} if error else {})}
        remaining = max_input - overhead - len(_encode({**content, "evidence": {}})) - 64
        if remaining < 0:
            raise ValueError("Locked authority/metadata exceed the input budget; they cannot be silently truncated.")
        # One budget: current requested evidence first, then recent context and
        # optional overviews. Reserving space for unrelated small files starves
        # the actual comparison and can silently evict a newly selected section.
        content["evidence_read_budget_chars"] = max(0, remaining - 256)
        # Keep source/native overviews beside focused reads when they fit; a
        # new window must not erase the context needed to interpret its sections.
        selected = dict(working)
        if len(_encode(selected)) > remaining - 64:
            # New observations/corrections can reduce this window's budget.
            # Evict old context, not the review: only actually sent sections
            # count as read, and omitted sections remain available on demand.
            selected, _, _ = evidence.window([], remaining - 64, working)
            working = selected
        # Small measurements/controls precede bulky sources. DAG responsibility
        # remains in producer_map; upstream ownership is not evidence priority.
        for name in sorted((views.keys() - verifications.keys()) & (native_views | evidence.delivered), key=lambda name: (
                          name not in native_views, name not in evidence.delivered,
                          name in evidence.bulk, len(views[name]), name)):
            if any(block.get("json_pointer") == "" and "start_char" not in block for block in working.get(name, [])):
                continue  # A complete requested value needs no duplicate overview.
            entry = next((item for item in evidence.inventory if item["path"] == name), {})
            if entry.get("line_count") and evidence.covered(
                    (name, 1, entry["line_count"]),
                    [evidence.block_selector(name, block) for block in working.get(name, [])]):
                continue  # Do not double the complete text and evict its comparison peer.
            candidate = {**selected, name: {"sections": working[name], "overview": views[name]} if name in working else views[name]}
            if len(_encode(candidate)) <= remaining - 64:
                selected = candidate
        content["evidence"] = selected
        if overhead + len(_encode(content)) > max_input:
            raise ValueError("Encoded review input exceeds the configured full-message budget.")
        return content

    while True:
        parsed = {}
        try:
            if calls >= max_calls or elapsed >= max_seconds:
                return session_error("authority_review_session_exhausted",
                    "Review session budget exhausted; preserve assets and the listed unresolved evidence. "
                    "Resume review with an explicitly extended budget; redispatch must not reset this session.")
            assembled = assemble()
            # Scope/bytes/config were checked when restoring this session.
            # Adding our own error text to the next prompt is not new evidence
            # and must not buy another identical output-budget retry.
            if exhausted_request:
                return session_error("authority_reviewer_output_exhausted",
                    "This unchanged review already exhausted its output budget. Preserve assets; change review configuration before resuming.")
            calls += 1
            call_output, call_reasoning, system_prompt = max_output, reasoning, _SYSTEM_PROMPT
            call_timeout = min(getattr(client, "timeout", 300), max_seconds - elapsed)
            # A stalled read must fail before the enclosing logical call, so
            # the existing transport retry can run against the SAME evidence.
            transport_timeout = min(request_timeout, call_timeout / 3)
            if hasattr(client, "retry_budget_s"):
                client.retry_budget_s = min(call_timeout, retry_budget) if retry_budget is not None else call_timeout
            emit_progress(state, "authority_review", "reviewing materialized assets", terminal=calls == initial_calls + 1,
                          attempt=calls, mode="focused" if working else "full", payload_chars=len(_encode(assembled)),
                          total_input_chars=len(system_prompt) + len(_encode([_REVIEW_TOOL])) + len(_encode(assembled)), max_input_chars=max_input,
                          evidence_capacity_chars=assembled["evidence_read_budget_chars"],
                          evidence_chars=len(_encode(assembled["evidence"])),
                          max_output_tokens=call_output, reasoning_effort=call_reasoning or "provider_default",
                          call_timeout_s=round(call_timeout, 3), request_timeout_s=round(transport_timeout, 3))
            checkpoint()
            response = None
            started = time.monotonic()
            try:
                with model_call_timing() as timing:
                    async with asyncio.timeout(call_timeout):
                        response = await client.chat(
                            [LLMMessage(role="system", content=system_prompt), LLMMessage(role="user", content=_encode(assembled))],
                            # Inherit the configured client transport policy and reuse this exact evidence window;
                            # the enclosing deadline also bounds retry/backoff time.
                            tools=[_REVIEW_TOOL], max_tokens=call_output, temperature=0.1,
                            timeout=transport_timeout,
                            **reasoning_options(client, call_reasoning))
            finally:
                elapsed += time.monotonic() - started
                checkpoint()
                emit_progress(state, "authority_review_call_done", terminal=False,
                              elapsed_s=round(time.monotonic() - started, 1),
                              response_complete=response is not None,
                              finish_reason=getattr(response, "finish_reason", None),
                              completion_tokens=(getattr(response, "usage", None) or {}).get("completion_tokens"), **timing)
            usage = response.usage or {}
            state.tokens_used += int(usage.get("total_tokens") or 0)
            arguments = next(((call.get("function") or {}).get("arguments") for call in response.tool_calls or []
                              if (call.get("function") or {}).get("name") == "submit_authority_review"), None)
            raw = arguments or response.content or ""
            state.append_transcript("authority_review_response", finish_reason=response.finish_reason,
                                    completion_tokens=usage.get("completion_tokens"),
                                    reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                                    content_chars=len(raw), reasoning_effort=call_reasoning or "provider_default")
            if response.tool_calls and not arguments and not str(raw).strip():
                raise ValueError("Only submit_authority_review is callable. Put requested_files and verification_requests "
                                 "inside its single JSON arguments object; do not call these field names as tools.")
            if response.finish_reason == "length" and not str(raw).strip():
                if reasoning != "none" and reasoning_options(client, "none"):
                    # Some providers spend the entire output allowance on
                    # hidden reasoning. Retain evidence and cumulative costs,
                    # but request a usable verdict within the SAME limits.
                    reasoning, exhausted_request = "none", ""
                    error = "Previous response contained no verdict. Return a concise verdict or the precise missing evidence."
                    checkpoint()
                    continue
                exhausted_request = scope
                error = "Output budget exhausted without a decision."
                return session_error("authority_reviewer_output_exhausted",
                    "No verdict was produced within the configured output budget. Preserve assets and observations; "
                    "change model/reasoning configuration or explicitly extend the budget before resuming.")
            # A complete verdict is usable even if the provider labels its stop
            # as length. Malformed/truncated JSON gets one short protocol repair.
            parsed = _extract_object(raw)
            state.append_transcript("authority_review_decision", status=parsed.get("status"),
                summary=parsed.get("summary"), requested_files=parsed.get("requested_files") or [],
                verification_requests=[{key: item[key] for key in ("tool", "condition", "scope", "files") if key in item}
                                       for item in parsed.get("verification_requests") or [] if isinstance(item, dict)],
                condition_checks=parsed.get("condition_checks") or [])
            # Stable identities survive description changes. Legacy checkpoints
            # acquire IDs here too; omission still cannot erase an open finding.
            prior_checks = draft.get("condition_checks") or []
            known_ids = {item["condition_id"] for item in prior_checks}
            prior_labels = {item["condition"]: item["condition_id"] for item in prior_checks}
            for item in parsed.get("condition_checks") or []:
                if not isinstance(item, dict) or not isinstance(item.get("condition"), str):
                    continue
                item["condition_id"] = (item["condition_id"] if item.get("condition_id") in known_ids
                    else prior_labels.get(item["condition"]) or canonical_hash(item["condition"])[:16])
            for field, identity in (("condition_checks", "condition_id"), ("asset_bindings", "asset_id")):
                if not isinstance(parsed.get(field, []), list):
                    continue  # Leave malformed fields for the verdict validator.
                updates = parsed.get(field) or []
                updated = {item[identity] for item in updates
                           if isinstance(item, dict) and isinstance(item.get(identity), str)}
                inherited = [item for item in draft.get(field) or []
                             if isinstance(item, dict) and isinstance(item.get(identity), str)
                             and item[identity] not in updated]
                if inherited:
                    parsed[field] = inherited + updates
            # An actionable issue closes review. A failed condition alone may
            # still need a cross-file read to locate its repair owner; use the
            # existing bounded evidence loop instead of rejecting that request.
            result = _verdict_check(parsed, evidence, requirement_id, pending, producer_ids)
            verification_requests = parsed.get("verification_requests") or []
            if verification_requests and result["status"] != "fail":
                if (_status(parsed.get("status")) != "needs_evidence" or not isinstance(verification_requests, list)
                        or len(verification_requests) > 2):
                    raise ValueError("Request at most two targeted verifications using status=needs_evidence.")
                acquired: list[str] = []
                draft = _completed_observations(parsed)
                for request in verification_requests:
                    name = await acquire(request)
                    if name:
                        acquired.append(name)
                checkpoint()
                # New results are requested evidence, not optional overviews:
                # page them first even if the full receipt cannot fit. A stale
                # read queue must not preempt delivery of the new measurement.
                if acquired:
                    requests = parsed.get("requested_files") or []
                    if not isinstance(requests, list):
                        raise ValueError("requested_files must be an array.")
                    # Read the computed result first, not another copy of the
                    # model's Python source. The complete receipt stays readable.
                    parsed["requested_files"] = [
                        {"file": name, "json_pointer": "/result"}
                        if verifications[name].get("tool") == "execute_python" else name
                        for name in acquired
                    ] + evidence.queue_reads(pending_reads, requests)
            if result["status"] == "needs_evidence":
                requests = parsed.get("requested_files") or []
                if not isinstance(requests, list) or not (requests or pending_reads):
                    raise ValueError("needs_evidence requires an existing inventory path, pointer or line range.")
                draft = _completed_observations(parsed)
                # Continue repeated requests from their unread remainder, but
                # let the model narrow/replace the question instead of forcing
                # it to finish obsolete full-file reads.
                queued = evidence.queue_reads(pending_reads, requests)
                old_working = working
                old_pending = pending_reads
                working = {}
                pending_reads = queued
                try:
                    budget = assemble()["evidence_read_budget_chars"]
                    next_working, next_pending, next_reads = evidence.window(
                        queued, budget - 64, old_working)
                finally:
                    working = old_working
                    pending_reads = old_pending
                # Re-reading evicted evidence is valid. The cumulative session
                # budget bounds repeated requests without a second progress gate.
                working, pending_reads = next_working, next_pending
                assemble()
                error, corrections = "", 0
                checkpoint()
                state.append_transcript("authority_review_repair", reason="requested_evidence", sections=next_reads)
                continue
            result.update(model=getattr(client, "model", "") or "inherited_data_model", review_attempts=calls,
                          review_elapsed_s=round(elapsed, 3),
                          **({"verification_evidence": verifications} if verifications else {}))
            if result["status"] == "fail" and verifications:
                # Carry the actual check back to its owner, not just the reviewer's paraphrase.
                for issue in result["issues"]:
                    related = {name: receipt for name, receipt in verifications.items()
                               if issue.get("file") in receipt.get("inputs", {})
                               or issue.get("source_file") in receipt.get("inputs", {})}
                    if related:
                        issue["diagnostic_context"] = {**(issue.get("diagnostic_context") or {}),
                                                       "verification_evidence": related}
                result["revision_contract"] = revision_contract(result["issues"])
            if result["status"] != "needs_input":
                state.hook_state["_data_authority_review"] = {
                    "scope": scope, "authority": authority, "fingerprints": asset_fingerprints,
                    "delivery_fingerprints": delivery_fingerprints, "context_fingerprints": context_fingerprints,
                    "result": deepcopy(result)}
                state.hook_state["_data_authority_review"]["reference_sources"] = deepcopy(references)
            emit_progress(state, "authority_review_done", status=result["status"], calls=calls)
            return result
        except Exception as exc:
            checkpoint()
            if elapsed >= max_seconds:
                return session_error("authority_review_session_exhausted",
                    "Review session time budget exhausted; preserve assets and the listed unresolved evidence. "
                    "Resume with an explicitly extended budget; redispatch must not reset this session.")
            if isinstance(exc, (TimeoutError, LLMHTTPError)) or is_transient_provider_error(exc):
                timed_out = isinstance(exc, (TimeoutError, httpx.TimeoutException))
                return {**session_error("authority_reviewer_unavailable",
                    f"Review model call failed: {type(exc).__name__}: "
                    f"{str(exc) or 'no complete response before the call/session deadline'}"),
                    "failure_category": "model_service_timeout" if timed_out else "model_service_unavailable"}
            if isinstance(exc, BufferError):
                # Reader capacity is not a malformed model verdict or a scientific defect.
                # Preserve the assets/session without asking the model to repeat the same read.
                return session_error("authority_review_evidence_budget", str(exc))
            if isinstance(exc, OSError):
                return session_error("authority_reviewer_unavailable", f"Review service/evidence failed: {type(exc).__name__}: {exc}")
            if corrections >= 1 or calls == 0:
                return session_error("authority_reviewer_output_exhausted" if exhausted_request else "authority_reviewer_failed",
                    f"Review could not return a usable verdict; assets are preserved. {type(exc).__name__}: {exc}")
            corrections += 1
            error = f"{type(exc).__name__}: {exc}"
            if parsed:
                # Keep diagnostic observations, not a rejected selector that
                # would invite the model to request the same nonexistent path.
                draft = _completed_observations({**parsed, "requested_files": [], "verification_requests": []})
            state.append_transcript("authority_review_repair", reason="invalid_review_response", error=error)
            # Preserve the last delivered window and completed observations;
            # malformed responses do not reset the evidence session.
            checkpoint()


def combine_reviews(deterministic_review: dict[str, Any], authority_review: dict[str, Any]) -> dict[str, Any]:
    """One verdict; diagnostics and semantic observations remain distinguishable."""
    issues = [*(deterministic_review.get("issues") or []), *(authority_review.get("issues") or [])]
    passed = deterministic_review.get("status") == "pass" and authority_review.get("status") == "pass"
    common = dict(deterministic_review.get("common_checks") or {})
    for requirement in authority_review.get("requirement_checks") or []:
        for check in requirement.get("condition_checks") or []:
            common[f"authority_condition:{check['condition_id']}"] = check.get("status") == "pass"
    result = {**deterministic_review, "status": "pass" if passed else "fail", "issues": issues,
              "common_checks": common, "authority_review": authority_review}
    if not passed:
        result["revision_contract"] = revision_contract(issues)
    return result
