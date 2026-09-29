"""Versioned run-local persistence and approval integrity for planning artifacts."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.state import State
from nodes.data.pipeline_contract import revision_contract
from nodes.data.planning.schemas import tool_allowed_in_plan_kind


def canonical_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PlanningStore:
    def __init__(self, state: State) -> None:
        self.state = state
        self.root = state.root / "planning"
        self.root.mkdir(parents=True, exist_ok=True)

    def _versions(self, prefix: str) -> list[Path]:
        def version(path: Path) -> int:
            try:
                return int(path.stem.rsplit("_", 1)[1])
            except (IndexError, ValueError):
                return -1

        return sorted(
            (path for path in self.root.glob(f"{prefix}_*.json") if version(path) >= 0),
            key=version,
        )

    def _next_version(self, prefix: str) -> int:
        paths = self._versions(prefix)
        return int(paths[-1].stem.rsplit("_", 1)[1]) + 1 if paths else 0

    def _write(self, path: Path, value: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def reference_state_path(self) -> Path:
        return self.root / "reference_state.json"

    def revision_state_path(self) -> Path:
        """Return the single persisted generation-revision record for this run."""
        return self.root / "generation_revision.json"

    def load_generation_revision(self) -> dict[str, Any]:
        path = self.revision_state_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def open_generation_revision(
        self,
        *,
        plan_id: str,
        plan_hash: str,
        feedback: Any,
    ) -> dict[str, Any]:
        """Persist one immutable, targeted revision request.

        Package review is a request to amend a known plan, not permission to
        restart planning. The signature makes unchanged feedback observable;
        attempt count is diagnostic and never acts as a fixed retry limit.
        """
        # The same actionable finding is the same revision request even when
        # a failed amendment created a new plan id.  Signing plan identity as
        # well as feedback made unchanged failures look new and allowed an
        # endless plan-A/plan-B repair loop.
        signature = str(
            feedback.get("revision_signature") or ""
        ) if isinstance(feedback, dict) else ""
        if not signature:
            signature_feedback = (
                feedback.get("feedback") or feedback.get("issues") or feedback
                if isinstance(feedback, dict) else feedback
            )
            contract = revision_contract(signature_feedback)
            signature = contract["signature"] if contract else canonical_hash({"feedback": signature_feedback})
        path = self.revision_state_path()
        current = self.load_generation_revision()
        if isinstance(current, dict) and current.get("signature") == signature:
            return current
        record = {
            "signature": signature,
            "base_plan_id": str(plan_id or ""),
            "base_plan_hash": str(plan_hash or ""),
            "feedback": feedback,
            "attempts": 0,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(path, record)
        return record

    def begin_generation_revision(self, signature: str) -> dict[str, Any] | None:
        """Record an amendment attempt; progress checks decide whether to continue."""
        path = self.revision_state_path()
        record = self.load_generation_revision()
        if not isinstance(record, dict) or record.get("signature") != signature:
            return None
        record["attempts"] = int(record.get("attempts") or 0) + 1
        record["status"] = "in_progress"
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(path, record)
        return record

    def close_generation_revision(self, signature: str, *, status: str) -> None:
        path = self.revision_state_path()
        record = self.load_generation_revision()
        if not isinstance(record, dict) or record.get("signature") != signature:
            return
        record["status"] = status
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(path, record)

    def load_reference_state(self) -> dict[str, Any]:
        """Load the single reference ledger/evidence state for this run."""
        path = self.reference_state_path()
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    value.setdefault("version", 1)
                    value.setdefault("gaps", {})
                    value.setdefault("evidence", [])
                    value.setdefault("transition_signatures", {})
                    return value
            except (OSError, json.JSONDecodeError):
                pass
        return {"version": 1, "gaps": {}, "evidence": [], "plan_signature": "", "transition_signatures": {}}

    def save_reference_state(
        self,
        gaps: dict[str, Any],
        evidence: list[dict[str, Any]] | None = None,
        plan_signature: str | None = None,
    ) -> None:
        current = self.load_reference_state()
        self._write(self.reference_state_path(), {
            "version": 1,
            "gaps": gaps,
            "evidence": (evidence or [])[:20],
            "plan_signature": current.get("plan_signature", "") if plan_signature is None else plan_signature,
            "transition_signatures": current.get("transition_signatures") or {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def record_reference_outcome(
        self,
        gap_id: str,
        *,
        status: str,
        fields: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one gap transition and its evidence in a single write.

        Planner code must not update the ledger and evidence independently:
        an interrupted run otherwise leaves a gap transition without the
        evidence that justified it (or vice versa).
        """
        state = self.load_reference_state()
        gaps = state.get("gaps") if isinstance(state.get("gaps"), dict) else {}
        key = str(gap_id or "").strip()
        current = gaps.get(key) if isinstance(gaps.get(key), dict) else {}
        updated = {**current, **(fields or {}), "status": str(status)}
        gaps[key] = updated
        previous = [item for item in state.get("evidence") or [] if isinstance(item, dict)]
        if isinstance(evidence, dict):
            evidence_key = str(evidence.get("gap_id") or evidence.get("step_id") or key)
            previous = [
                item for item in previous
                if str(item.get("gap_id") or item.get("step_id") or "") != evidence_key
            ]
            previous.append(evidence)
        self.save_reference_state(
            gaps,
            previous,
            plan_signature=state.get("plan_signature"),
        )
        return updated

    def commit_reference_ledger(self, gaps: dict[str, Any], *, plan_signature: str | None = None) -> None:
        """Persist the current gap ledger while retaining evidence/signature."""
        state = self.load_reference_state()
        self.save_reference_state(
            gaps,
            state.get("evidence") or [],
            plan_signature=state.get("plan_signature") if plan_signature is None else plan_signature,
        )

    def transition_signature_seen(self, signature: str, *, ledger_signature: str = "") -> bool:
        """Return whether an unchanged pipeline transition was already seen."""
        state = self.load_reference_state()
        transitions = state.get("transition_signatures")
        if not isinstance(transitions, dict):
            return False
        record = transitions.get(str(signature))
        return isinstance(record, dict) and str(record.get("ledger_signature") or "") == str(ledger_signature)

    def record_transition_signature(
        self,
        signature: str,
        *,
        plan_id: str = "",
        status: str = "",
        ledger_signature: str = "",
    ) -> None:
        state = self.load_reference_state()
        transitions = state.get("transition_signatures")
        if not isinstance(transitions, dict):
            transitions = {}
        transitions[str(signature)] = {
            "plan_id": plan_id,
            "status": status,
            "ledger_signature": ledger_signature,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(self.reference_state_path(), {
            "version": 1,
            "gaps": state.get("gaps") or {},
            "evidence": (state.get("evidence") or [])[:20],
            "plan_signature": state.get("plan_signature") or "",
            "transition_signatures": transitions,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    @staticmethod
    def _plan_kind(record: dict[str, Any]) -> str:
        plan = record.get("plan") or {}
        explicit = str(plan.get("plan_kind") or "").strip()
        if explicit:
            return explicit
        steps = plan.get("generation_steps") or []
        if steps and all(
            isinstance(step, dict)
            and tool_allowed_in_plan_kind(step.get("tool_name"), "reference_evidence_only")
            for step in steps
        ):
            return "reference_evidence_only"
        return "preprocessing_generation"

    def save_plan(self, plan: dict[str, Any], *, source: str) -> dict[str, Any]:
        plan_hash = canonical_hash(plan)
        # Reuse only the current plan of the same kind.  Returning to an
        # earlier draft after a newer revision (A -> B -> A) is a real state
        # transition: approval integrity intentionally requires the selected
        # plan to be the latest version.  Reusing the historical A here would
        # create an approval for a stale plan id and silently prevent execution.
        plan_kind = self._plan_kind({"plan": plan})
        existing = self.latest_plan(plan_kind)
        if existing and existing.get("plan_hash") == plan_hash:
            self.state.append_transcript(
                "preprocessing_plan_reused",
                plan_id=existing["plan_id"],
                plan_hash=plan_hash,
                source=source,
            )
            return {**existing, "reused": True}
        version = self._next_version("plan")
        record = {
            "version": version,
            "plan_id": f"plan_{version:02d}",
            "plan_hash": plan_hash,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "plan": plan,
        }
        path = self.root / f"plan_{version:02d}.json"
        self._write(path, record)
        self.state.append_transcript(
            "preprocessing_plan_saved",
            plan_id=record["plan_id"],
            plan_hash=record["plan_hash"],
            source=source,
        )
        return {**record, "path": str(path), "reused": False}

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        wanted = str(plan_id or "").strip()
        if not wanted:
            return None
        for path in reversed(self._versions("plan")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if str(record.get("plan_id") or "") == wanted:
                return {**record, "path": str(path)}
        return None

    def latest_plan(self, plan_kind: str | None = None) -> dict[str, Any] | None:
        paths = self._versions("plan")
        for path in reversed(paths):
            record = json.loads(path.read_text(encoding="utf-8"))
            if plan_kind is None or self._plan_kind(record) == plan_kind:
                return {**record, "path": str(path)}
        return None

    def latest_critique(self, plan_hash: str = "") -> dict[str, Any] | None:
        for path in reversed(self._versions("critique")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if not plan_hash or record.get("plan_hash") == plan_hash:
                return {**record, "path": str(path)}
        return None

    def save_critique(self, critique: dict[str, Any], plan_record: dict[str, Any]) -> dict[str, Any]:
        critique_hash = canonical_hash(critique)
        existing = self.latest_critique(plan_record["plan_hash"])
        if existing and canonical_hash(existing.get("critique") or {}) == critique_hash:
            self.state.append_transcript(
                "preprocessing_critique_reused",
                critique_id=existing["critique_id"],
                plan_id=plan_record["plan_id"],
            )
            return {**existing, "reused": True}
        version = self._next_version("critique")
        record = {
            "version": version,
            "critique_id": f"critique_{version:02d}",
            "plan_id": plan_record["plan_id"],
            "plan_hash": plan_record["plan_hash"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "critique": critique,
        }
        path = self.root / f"critique_{version:02d}.json"
        self._write(path, record)
        if critique.get("decision") == "approve":
            kind = str((plan_record.get("plan") or {}).get("plan_kind") or "preprocessing_generation")
            approval_name = "approved_reference_plan.json" if kind == "reference_evidence_only" else "approved_plan.json"
            self._write(self.root / approval_name, {
                "approved_at": record["created_at"],
                "plan_id": record["plan_id"],
                "plan_hash": record["plan_hash"],
                "critique_id": record["critique_id"],
                "overall_score": critique.get("overall_score"),
                "plan": plan_record["plan"],
            })
            self.state.hook_state[
                "approved_reference_plan_hash" if kind == "reference_evidence_only" else "approved_preprocessing_plan_hash"
            ] = record["plan_hash"]
        self.state.append_transcript(
            "preprocessing_plan_critiqued",
            critique_id=record["critique_id"],
            plan_id=record["plan_id"],
            decision=critique.get("decision"),
            overall_score=critique.get("overall_score"),
        )
        return {**record, "path": str(path), "reused": False}

    def approval_status(self, plan_kind: str | None = None) -> dict[str, Any]:
        target_kind = plan_kind or "preprocessing_generation"
        latest = self.latest_plan(target_kind)
        approval_path = self.root / ("approved_reference_plan.json" if target_kind == "reference_evidence_only" else "approved_plan.json")
        if latest is None:
            return {"approved": False, "reason": "no preprocessing plan exists"}
        if not approval_path.exists():
            return {"approved": False, "reason": "latest plan has not passed critique", "latest_plan_id": latest["plan_id"]}
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        actual_hash = canonical_hash(latest["plan"])
        approved = (
            approval.get("plan_id") == latest.get("plan_id")
            and approval.get("plan_hash") == latest.get("plan_hash") == actual_hash
        )
        if approved and self.is_plan_invalidated(target_kind, str(approval.get("plan_id") or "")):
            approved = False
        return {
            "approved": approved,
            "reason": "approved" if approved else "approved plan is stale or was modified",
            "latest_plan_id": latest.get("plan_id"),
            "latest_plan_hash": actual_hash,
            "approved_plan_id": approval.get("plan_id"),
            "approved_plan_hash": approval.get("plan_hash"),
            "overall_score": approval.get("overall_score"),
        }

    def clear_approval(self, plan_kind: str = "preprocessing_generation") -> None:
        """Invalidate the current approval after execution requires replanning."""
        name = "approved_reference_plan.json" if plan_kind == "reference_evidence_only" else "approved_plan.json"
        path = self.root / name
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self.state.append_transcript(
                "preprocessing_plan_approval_clear_failed",
                plan_kind=plan_kind,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def invalidate_plan(self, plan_kind: str, plan_id: str, reason: str = "execution_failed") -> None:
        """Record a failed plan id so it cannot be silently re-approved."""
        wanted = str(plan_id or "").strip()
        if not wanted:
            return
        path = self.root / "invalidated_plan_ids.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            value = {}
        ids = value.get(plan_kind) if isinstance(value, dict) else []
        ids = [str(item) for item in ids or [] if str(item)]
        already_invalidated = wanted in ids
        if wanted not in ids:
            ids.append(wanted)
        self._write(path, {**(value if isinstance(value, dict) else {}), plan_kind: ids[-50:]})
        self.clear_approval(plan_kind)
        if not already_invalidated:
            self.state.append_transcript("preprocessing_plan_invalidated", plan_id=wanted, plan_kind=plan_kind, reason=reason)

    def is_plan_invalidated(self, plan_kind: str, plan_id: str) -> bool:
        path = self.root / "invalidated_plan_ids.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return False
        return str(plan_id or "") in {str(item) for item in (value.get(plan_kind) or [])}


def witness_plan_approval(state: State, operation: str) -> dict[str, Any]:
    """判决拆除 O9 根（store:468 降格，2026-08-31）。

    Designer/Critic 评审照跑照记，但**不再是通行许可**：未评审的执行不被拒绝，
    它被如实记录，且其每件产物必须打上 ``plan_approval_status: unapproved``。
    本函数返回应并入产物/结果的戳记；原 ``require_approved_plan`` 的
    ~30 个 needs_plan_approval/needs_plan_execution 返回点随根一并转为记录。
    """
    status = PlanningStore(state).approval_status()
    approved = bool(status.get("approved"))
    stamp: dict[str, Any] = {
        "plan_approval_status": "approved" if approved else "unapproved",
    }
    if not approved:
        try:
            state.append_transcript(
                "preprocessing_unreviewed_execution",
                operation=operation,
                reason=status.get("reason"),
                planning_status=status,
            )
        except Exception:
            pass
    return stamp


def approved_plan(state: State) -> dict[str, Any] | None:
    store = PlanningStore(state)
    if not store.approval_status().get("approved"):
        return None
    status = store.approval_status("preprocessing_generation")
    latest = store.get_plan(status.get("approved_plan_id")) if status.get("approved") else None
    return latest.get("plan") if latest else None


def plan_authorizes_tool(plan: dict[str, Any], tool_name: str) -> bool:
    return any(
        isinstance(requirement, dict)
        and tool_name in (requirement.get("selected_tools") or [])
        for requirement in plan.get("tool_requirements") or []
    )
