"""Scoped references to prior typed reviews, never scientific approval authority.

The reviewer declares the dependency boundary it actually checked. Framework
snapshots bind that declaration to current question/evidence bytes and the
delivered rubric; only an explicit current reviewer action can carry it forward.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import stat
from pathlib import Path
from typing import Any

ROLES = ("proof", "data", "definitions", "criteria", "use")
STATUSES = ("verified", "carried_forward", "reopened", "unverified")
_WINDOWS = os.name == "nt"


def _windows_fd_path(fd: int) -> str:
    """Query the opened handle, not its potentially replaced input pathname."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # https://learn.microsoft.com/windows/win32/api/fileapi/
    # nf-fileapi-getfinalpathnamebyhandlew (normalized DOS/UNC path, flags=0).
    query = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    query.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    query.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(fd)
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = query(handle, buffer, size, 0)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        if length < size:
            return buffer.value
        size = length + 1


def _windows_path_key(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))


def _open_dependency(path: Path) -> int:
    """No bytes are read until this exact descriptor is verified."""
    if _WINDOWS:
        fd = os.open(path, os.O_RDONLY | os.O_BINARY)
        try:
            if _windows_path_key(_windows_fd_path(fd)) != _windows_path_key(str(path)):
                raise ValueError("dependency opened handle escaped its validated path")
            return fd
        except BaseException:
            os.close(fd)
            raise
    # Walk from a stable filesystem root. O_NOFOLLOW on *every* component
    # closes parent-directory symlink/ABA windows, not only leaf substitution.
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path.anchor, directory_flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def _file_hash(path: Path) -> str:
    fd = _open_dependency(path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("dependency must be a regular file")

        def check_identity():
            current = path.lstat()
            if (
                path.resolve(strict=True) != path
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise ValueError("dependency path changed after boundary validation")

        check_identity()
        stream = os.fdopen(fd, "rb")
        fd = None  # stream owns the same validated descriptor from here on.
        digest = hashlib.sha256()
        with stream:
            while chunk := stream.read(65536):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            if any(
                getattr(before, key) != getattr(after, key)
                for key in ("st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise ValueError("dependency file changed while hashing")
            check_identity()
        return digest.hexdigest()
    finally:
        if fd is not None:
            os.close(fd)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _scope(state) -> dict:
    root = getattr(state, "project_worktree", None) or state.root
    return {
        "root": str(Path(root).resolve()),
        "tenant_id": getattr(state, "tenant_id", None),
        "project_id": getattr(state, "project_id", None),
    }


def _record(state, artifact_id: str) -> dict:
    from core.ledger import sha256_text

    record = state.read_artifact(artifact_id)
    if not record:
        raise ValueError(f"missing dependency artifact: {artifact_id}")
    if sha256_text(record.get("content") or "") != record.get("content_hash"):
        raise ValueError(f"dependency content hash mismatch: {artifact_id}")
    return {**record, "id": record.get("artifact_id") or artifact_id}


def _question(record: dict, question_id: str) -> dict:
    rows = (record.get("metadata") or {}).get("hypotheses") or []
    found = [row for row in rows if isinstance(row, dict) and row.get("id") == question_id]
    if len(found) != 1:
        raise ValueError(f"missing or ambiguous question identity: {question_id}")
    return found[0]


def _selected(record: dict, reference: dict) -> Any:
    value = (
        _question(record, reference["question_id"])
        if reference.get("question_id")
        else {"content": record["content"], "metadata": record.get("metadata") or {}}
    )
    pointer = reference.get("pointer", "")
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("dependency pointer must be an RFC 6901 JSON pointer")
    array_items = []
    for token in pointer.split("/")[1:]:
        if re.search(r"~(?![01])", token):
            raise ValueError("invalid dependency pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", token):
                    raise ValueError("invalid dependency array index")
                value = value[int(token)]
                # A positional pointer cannot silently switch to another item
                # with equal leaf text after an array reorder.
                array_items.append(value)
            elif isinstance(value, dict):
                value = value[token]
            else:
                raise ValueError("dependency pointer does not select an object")
        except (KeyError, IndexError) as exc:
            raise ValueError(f"missing dependency pointer: {pointer}") from exc
    return {"value": value, "array_items": array_items}


def _dependency(state, reference: dict) -> dict:
    if not isinstance(reference, dict) or reference.get("role") not in ROLES:
        raise ValueError("dependency requires proof/data/definitions/criteria/use role")
    if bool(reference.get("artifact_id")) == bool(reference.get("path")):
        raise ValueError("dependency requires exactly one artifact_id or project path")
    if reference.get("artifact_id"):
        record = _record(state, reference["artifact_id"])
        selector = {key: reference[key] for key in ("question_id", "pointer") if key in reference}
        return {
            "role": reference["role"],
            "artifact_id": record["id"],
            "artifact_type": record["type"],
            "version": record["version"],
            **selector,
            "value_hash": _hash(_selected(record, reference)),
        }
    if reference.get("pointer") or reference.get("question_id"):
        raise ValueError("file dependencies cannot have JSON selectors")
    from core.project_workspace import resolve_tool_path

    path = resolve_tool_path(state, reference["path"])
    return {
        "role": reference["role"],
        "path": str(path),
        "value_hash": _file_hash(path),
    }


def _standards(state) -> dict:
    from core.naming import canonical_node_type
    from core.review_policy import REVIEW_POLICY_SHA256

    delivery = state.hook_state.get("_owner_review_spec_delivery") or {}
    if not delivery.get("owner_spec_loaded") or not delivery.get("owner_spec_sha256"):
        raise ValueError("question verification needs the delivered owner review standard")
    if delivery.get("owner_spec_truncated"):
        raise ValueError(
            "question verification cannot claim complete review of a truncated standard"
        )
    path = Path(delivery.get("owner_spec_path") or "")
    if (
        not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != delivery["owner_spec_sha256"]
    ):
        raise ValueError("delivered owner review standard changed; refresh reviewer context")
    return {
        "source_node_type": canonical_node_type(delivery["source_node_type"]),
        "owner_spec_sha256": delivery["owner_spec_sha256"],
        "review_policy_sha256": REVIEW_POLICY_SHA256,
    }


def snapshot(
    state, subject_id: str, question_id: str, dependencies: list, intended_use: str
) -> dict:
    subject = _record(state, subject_id)
    if subject.get("type") != "research_state":
        raise ValueError("question review currently applies to typed research_state only")
    row = _question(subject, question_id)
    if not isinstance(intended_use, str) or not intended_use.strip():
        raise ValueError("question review requires its current intended_use")
    if not isinstance(dependencies, list) or set(ROLES) - {
        ref.get("role") for ref in dependencies if isinstance(ref, dict)
    }:
        raise ValueError("complete bindings required for proof/data/definitions/criteria/use")
    checked = [_dependency(state, reference) for reference in dependencies]
    from core.naming import canonical_node_type

    standards = _standards(state)
    owner = canonical_node_type(subject.get("produced_by_node_type") or "")
    if owner != "analysis" or standards["source_node_type"] != owner:
        raise ValueError("delivered review standard does not match the research_state owner")
    # Explicit evidence links cannot be omitted from a completeness declaration.
    # Run IDs or unavailable sources require a limited/unverified finding.
    for evidence in row.get("evidence") or []:
        if state.read_artifact(evidence):
            checked.append(_dependency(state, {"role": "data", "artifact_id": evidence}))
        else:
            checked.append(_dependency(state, {"role": "data", "path": evidence}))
    comparable = [{k: v for k, v in item.items() if k != "version"} for item in checked]
    comparable.sort(key=lambda item: json.dumps(item, sort_keys=True))
    binding = {
        "binding_version": 1,
        "scope": _scope(state),
        "artifact_id": subject["id"],
        "question_id": question_id,
        "question_hash": _hash(row),
        "intended_use": intended_use.strip(),
        "standards": standards,
        "dependencies": comparable,
    }
    return {
        **binding,
        "subject_version": subject["version"],
        "subject_content_hash": subject["content_hash"],
        "dependency_versions": checked,
        "fingerprint": _hash(binding),
    }


def _previous(state, subject_id: str) -> tuple[dict, dict | None]:
    """Newest typed finding wins, including an explicit reopening; never fall back."""
    latest, legacy = {}, None
    for item in state.list_artifacts("review_critique"):
        # The ledger remains available when a body is missing or corrupt. Do
        # not skip a newer reopening and thereby resurrect an older pass.
        head = state.artifact_head(item["id"])
        owner = head.produced_by_node_type if head else item.get("owner_node")
        if owner not in {"_reviewer", "reviewer"}:
            continue
        metadata = head.metadata if head else {}
        stamped_scope = metadata.get("question_review_scope")
        if stamped_scope and stamped_scope != _scope(state):
            continue
        subject_stamp = metadata.get("review_subject") or {}
        stamped_id = subject_stamp.get("artifact_id") if isinstance(subject_stamp, dict) else None
        stamped_id = stamped_id or metadata.get("artifact_under_review")
        if stamped_id and stamped_id != subject_id:
            continue
        try:
            record = _record(state, item["id"])
            payload = json.loads(record["content"])
            if not isinstance(payload, dict):
                raise ValueError("review body is not a structured record")
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            reference = {
                "artifact_id": item["id"],
                "version": head.version if head else None,
                "content_hash": head.sha256 if head else None,
            }
            reason = f"Newer review {item['id']} is unavailable or corrupt: {exc}"
            legacy = {"status": "reference_only", "previous_review": reference, "reason": reason}
            for prior in latest.values():
                prior.update(status="unverified", reason=reason, blocking_review=reference)
            continue
        subject = payload.get("review_subject") or {}
        if not isinstance(subject, dict):
            continue
        if (
            payload.get("_composed_by")
            not in {"compose_review_critique", "reviewer_critique_lands_hook"}
            or subject.get("artifact_id") != subject_id
        ):
            continue
        if int(subject.get("version") or 0) > int(_record(state, subject_id)["version"]):
            continue
        reference = {
            "artifact_id": record["id"],
            "version": record["version"],
            "content_hash": record["content_hash"],
            "subject_version": subject.get("version"),
        }
        findings = payload.get("question_findings") or []
        if not findings:
            legacy = {
                "status": "reference_only",
                "previous_review": reference,
                "summary": str(payload.get("summary", ""))[:1200],
                "reason": (
                    "Legacy review has no bound question findings; "
                    "strengths are not verification credentials."
                ),
            }
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            if (
                finding.get("scope") == _scope(state)
                and finding.get("artifact_id") == subject_id
                and finding.get("question_id")
            ):
                latest[finding["question_id"]] = {**finding, "previous_review": reference}
                if payload.get("review_incomplete") or (record.get("metadata") or {}).get(
                    "review_incomplete"
                ):
                    latest[finding["question_id"]].update(
                        status="unverified",
                        reason="Prior reviewer run is incomplete; finding is reference only.",
                    )
                stamp = finding.get("snapshot") or {}
                rubric = payload.get("rubric_source") or {}
                if not (
                    payload.get("review_incomplete")
                    or (record.get("metadata") or {}).get("review_incomplete")
                ) and (
                    stamp.get("subject_content_hash") != subject.get("content_hash")
                    or any(
                        (stamp.get("standards") or {}).get(key) != rubric.get(key)
                        for key in ("owner_spec_sha256", "review_policy_sha256")
                    )
                ):
                    latest[finding["question_id"]].update(
                        status="unverified",
                        reason="Prior finding mismatches its typed subject/rubric stamp.",
                    )
    return latest, legacy


def assess(state, subject_id: str, prior: dict) -> dict:
    result = {
        "question_id": prior["question_id"],
        "previous_review": prior["previous_review"],
        "finding": prior.get("finding", ""),
        "intended_use": prior.get("intended_use", ""),
        "dependencies": prior.get("dependencies", []),
        "dependency_limitations": prior.get("dependency_limitations", ""),
        "blocking_review": prior.get("blocking_review"),
    }
    if (
        prior.get("status") not in {"verified", "carried_forward"}
        or prior.get("dependency_completeness") != "complete"
    ):
        return {
            **result,
            "status": "reopened",
            "reasons": [prior.get("reason") or "Prior check is unverified or reopened."],
        }
    try:
        current = snapshot(
            state, subject_id, prior["question_id"], prior["dependencies"], prior["intended_use"]
        )
    except (ValueError, OSError, RuntimeError) as exc:
        return {**result, "status": "reopened", "reasons": [str(exc)]}
    old = prior.get("snapshot") or {}
    reasons = []
    for key in ("question_hash", "standards", "intended_use", "scope"):
        if old.get(key) != current.get(key):
            reasons.append(f"{key} changed")
    for role in ROLES:
        before = [ref for ref in old.get("dependencies", []) if ref["role"] == role]
        after = [ref for ref in current["dependencies"] if ref["role"] == role]
        if before != after:
            reasons.append(f"{role} dependency changed")
    if old.get("fingerprint") != current["fingerprint"] and not reasons:
        reasons.append("question binding changed")
    return {**result, "status": "reopened" if reasons else "eligible", "reasons": reasons}


def context_section(state, node_inputs: dict) -> str | None:
    subject_id = node_inputs.get("artifact_id")
    state.hook_state["_question_review_subject_id"] = subject_id
    if not subject_id:
        return None
    record = state.read_artifact(subject_id)
    if not record or record.get("type") != "research_state":
        return None
    previous, legacy = _previous(state, subject_id)
    references = [assess(state, subject_id, prior) for prior in previous.values()]
    if legacy:
        references.append(legacy)
    return (
        "## Question-level prior review references\n"
        "These are evidence references, not current approval. Read the current research_state. "
        "Use record_question for each question: explicitly carry forward an eligible finding "
        "only for the same current intended use and complete dependency boundary. Reopen for new "
        "suspicions or independent spot checks and record why. Missing bindings remain unverified. "
        "Proof bindings must cover the actual full proof, not a title or a 'checked' summary. "
        "A mixed file is a whole-file dependency unless its Q2 proof can be selected exactly; "
        "otherwise declare limited/unverified, never silently omit the rest of the proof. "
        "Q2 may be retained while the overall verdict/recommendation remains revise for Q5.\n"
        + json.dumps(references, ensure_ascii=False, indent=2)
    )


def record_question(
    state,
    *,
    artifact_under_review: str,
    question_id: str,
    question_status: str,
    question_finding: str,
    question_dependencies: list,
    dependency_completeness: str,
    dependency_limitations: str,
    intended_use: str,
    question_reason: str,
    previous_review_id: str,
    previous_review_version: int | None,
) -> dict:
    if state.hook_state.get("_question_review_subject_id") != artifact_under_review:
        raise ValueError("question subject must match the current reviewer context")
    subject = _record(state, artifact_under_review)
    _question(subject, question_id)
    if question_status not in STATUSES or dependency_completeness not in {"complete", "limited"}:
        raise ValueError("invalid question status or dependency completeness")
    if not intended_use.strip():
        raise ValueError("current intended_use must be explicit")
    verified = question_status in {"verified", "carried_forward"}
    if verified and (dependency_completeness != "complete" or dependency_limitations.strip()):
        raise ValueError(
            "limited dependencies must remain unverified, not verified/carried_forward"
        )
    if verified and not question_finding.strip():
        raise ValueError("a verified question requires the actual checked finding")
    if not verified and not question_reason.strip():
        raise ValueError("reopened/unverified question requires a reason")
    if dependency_completeness == "limited" and not dependency_limitations.strip():
        raise ValueError("limited dependencies require explicit limitations")
    frame = {
        "artifact_id": artifact_under_review,
        "scope": _scope(state),
        "question_id": question_id,
        "status": question_status,
        "finding": question_finding,
        "dependencies": question_dependencies,
        "intended_use": intended_use.strip(),
        "dependency_completeness": dependency_completeness,
        "dependency_limitations": dependency_limitations,
        "reason": question_reason,
    }
    try:
        frame["snapshot"] = snapshot(
            state, artifact_under_review, question_id, question_dependencies, intended_use
        )
    except (ValueError, OSError, RuntimeError) as exc:
        if verified:
            raise ValueError(str(exc)) from exc
        frame["binding_error"] = str(exc)
    previous, _ = _previous(state, artifact_under_review)
    prior = previous.get(question_id)
    if prior and question_status != "carried_forward":
        assessment = assess(state, artifact_under_review, prior)
        if (
            question_status == "verified"
            and assessment["status"] == "eligible"
            and not question_reason.strip()
        ):
            raise ValueError(
                "Prior check is eligible: carry forward or explain the independent recheck"
            )
        frame["reopened_from"] = prior["previous_review"]
        frame["reopen_reasons"] = assessment["reasons"] or [question_reason]
    if question_status == "carried_forward":
        if (
            not prior
            or prior["previous_review"]["artifact_id"] != previous_review_id
            or prior["previous_review"]["version"] != previous_review_version
        ):
            raise ValueError(
                "previous_review must identify the latest bound question finding/version"
            )
        assessment = assess(state, artifact_under_review, prior)
        if assessment["status"] != "eligible":
            raise ValueError("Cannot carry forward: " + "; ".join(assessment["reasons"]))
        if frame["snapshot"]["fingerprint"] != prior["snapshot"]["fingerprint"]:
            raise ValueError("current intended use or declared dependencies changed")
        frame["previous_review"] = prior["previous_review"]
        frame["finding"] = prior["finding"]
    return frame


def validate_at_finalize(state, subject_id: str, findings: list) -> None:
    for finding in findings:
        if finding["artifact_id"] != subject_id:
            raise ValueError("question finding belongs to a different review subject")
        if finding["status"] not in {"verified", "carried_forward"}:
            continue
        current = snapshot(
            state,
            subject_id,
            finding["question_id"],
            finding["dependencies"],
            finding["intended_use"],
        )
        if current["fingerprint"] != finding["snapshot"]["fingerprint"]:
            raise ValueError(
                f"{finding['question_id']} changed after verification; reopen or recheck"
            )
        finding["snapshot"] = current
        if finding["status"] == "carried_forward":
            previous, _ = _previous(state, subject_id)
            prior = previous.get(finding["question_id"])
            if (
                not prior
                or prior["previous_review"] != finding["previous_review"]
                or assess(state, subject_id, prior)["status"] != "eligible"
            ):
                raise ValueError("prior question finding changed after carry-forward")
