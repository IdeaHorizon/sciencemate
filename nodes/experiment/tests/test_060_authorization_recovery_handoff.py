"""Executable handoff for the Core-owned run-scoped egress grant gap.

No test in this file performs network I/O.  The real Core permission API and
Experiment pre-spawn policy check are exercised against a real ``State``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core import capability_grants
from core.state import State
from nodes.experiment.tools import resource_fetch
from shared.tools import builtin


HOST = "gitlab.com"
OTHER_HOST = "data.example.net"


class SameRunAuthorizationRecoveryGuidanceGap(AssertionError):
    """The known grant-loss plus blocker-only recovery advice, and nothing else."""


def _new_state(tmp_path: Path) -> tuple[State, Path]:
    base_dir = tmp_path / "runs"
    state = State.new(
        node_type="experiment",
        base_dir=base_dir,
        session_id="authorization-recovery-owner",
    )
    return state, base_dir


def _reopen(state: State, base_dir: Path) -> State:
    return State.reopen(
        node_type="experiment",
        base_dir=base_dir,
        run_id=state.run_id,
        session_id=state.session_id,
    )


def _request(state: State, host: str) -> dict:
    reason = "fetch an exact declared source"
    result = asyncio.run(builtin._request_network_access(
        state,
        host=host,
        reason=reason,
        what_for=f"https://{host}/owner/project/archive.tar.gz",
    ))
    assert result["status"] == "pause", result
    assert result["pause_event"]["metadata"]["type"] == "capability_grant"
    assert result["pause_event"]["metadata"]["host"] == host
    # This is the durable event shape written by the real tool-call path and
    # consumed by Experiment after a reopen.  Grant authority itself must not
    # be reconstructed from this node-writable transcript entry.
    state.append_transcript(
        "tool_call",
        turn=1,
        name="request_network_access",
        args={"host": host, "reason": reason},
    )
    return result


def _answer(state: State, host: str, answer: str) -> bool:
    granted = capability_grants.grant_from_answer(
        state,
        host,
        answer,
        reason="fetch an exact declared source",
    )
    # Mirror the durable audit event emitted by pause_driver without treating
    # it as authority.  Core owns any future trusted restore checkpoint.
    state.append_transcript(
        "capability_grant_answered",
        host=host,
        granted=granted,
        answer_unreadable=False,
    )
    return granted


def _require(state: State, host: str) -> dict:
    return resource_fetch._require_egress_access(
        state,
        f"https://{host}/owner/project/archive.tar.gz",
        reason="fetch an exact declared source",
        what_for=f"https://{host}/owner/project/archive.tar.gz",
    )


def test_approved_exact_host_is_allowed_while_the_live_state_is_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the permission channel works before process recovery."""
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "")
    state, _base_dir = _new_state(tmp_path)
    _request(state, HOST)

    assert _answer(state, HOST, "允许") is True
    decision = _require(state, HOST)

    assert decision["allowed"] is True
    assert decision["authorized_by"] == "per_run_exact_grant"
    assert decision["per_run_exact_grant"] is True
    assert decision["policy"]["per_run_granted_hosts"] == [HOST]


def test_same_run_reopen_restores_the_grant_or_recommends_reauthorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-run recovery must not incorrectly advise that only blocking remains.

    Either acceptable Core contract passes this test:
    1. restore the exact approved host for this same run; or
    2. fail closed but recommend a fresh HITL request.

    The request tool remains callable; omitting it from the structured recovery
    result and recommending only ``report_blocker`` is the known gap.
    """
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "")
    state, base_dir = _new_state(tmp_path)
    _request(state, HOST)
    assert _answer(state, HOST, "允许") is True
    assert _require(state, HOST)["allowed"] is True

    reopened = _reopen(state, base_dir)
    assert reopened.run_id == state.run_id
    try:
        decision = _require(reopened, HOST)
    except resource_fetch.NetworkAccessRequired as exc:
        refusal = exc.as_result(kind="git")
        if (
            refusal.get("error_code") == "network_access_request_unresolved"
            and "request_network_access" not in refusal
            and refusal.get("next_action", {}).get("tool") == "report_blocker"
        ):
            raise SameRunAuthorizationRecoveryGuidanceGap(
                "approved exact-host grant was lost and recovery advises only blocking"
            )
        assert refusal["error_code"] == "network_access_required", refusal
        request = refusal.get("request_network_access") or {}
        assert request.get("arguments", {}).get("host") == HOST, refusal
        assert refusal.get("previous_grant_lapsed") is True, refusal
        assert "report_blocker" not in refusal["error"], refusal
    else:
        assert decision["allowed"] is True, decision
        assert decision["authorized_by"] == "per_run_exact_grant", decision


def test_direct_rerequest_after_reopen_can_restore_the_live_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: Core still accepts a fresh HITL request after reopen."""
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "")
    state, base_dir = _new_state(tmp_path)
    _request(state, HOST)
    assert _answer(state, HOST, "允许") is True

    reopened = _reopen(state, base_dir)
    assert capability_grants.is_granted(reopened, HOST) is False

    second_request = _request(reopened, HOST)
    assert second_request["pause_event"]["metadata"]["host"] == HOST
    assert _answer(reopened, HOST, "允许") is True
    decision = _require(reopened, HOST)

    assert decision["allowed"] is True
    assert decision["authorized_by"] == "per_run_exact_grant"


def test_explicit_denial_remains_fail_closed_after_same_run_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: recovery must never turn a denial into permission."""
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "")
    state, base_dir = _new_state(tmp_path)
    _request(state, HOST)
    assert _answer(state, HOST, "拒绝") is False

    reopened = _reopen(state, base_dir)
    with pytest.raises(resource_fetch.NetworkAccessRequired) as caught:
        _require(reopened, HOST)
    refusal = caught.value.as_result(kind="git")

    assert refusal["error_code"] == "network_access_request_unresolved"
    assert "request_network_access" not in refusal
    assert refusal["next_action"]["tool"] == "report_blocker"
    assert capability_grants.is_granted(reopened, HOST) is False


def test_request_history_for_one_host_does_not_block_another_after_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: request and grant scope remains one exact hostname."""
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "")
    state, base_dir = _new_state(tmp_path)
    _request(state, HOST)
    assert _answer(state, HOST, "允许") is True

    reopened = _reopen(state, base_dir)
    with pytest.raises(resource_fetch.NetworkAccessRequired) as caught:
        _require(reopened, OTHER_HOST)
    refusal = caught.value.as_result(kind="file")

    assert refusal["error_code"] == "network_access_required", refusal
    assert refusal["request_network_access"]["arguments"]["host"] == OTHER_HOST
    assert refusal["egress_decision"]["per_run_exact_grant"] is False


def test_an_approval_that_never_takes_effect_on_the_live_run_does_not_spin_cards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1069：re-request is the exit for a grant lost with a restart, not a loop.

    After one lapsed-approval re-request on the same live State, a further
    approval that still does not take effect is a write-back failure, not a
    restart; advising another request would pop one card per human click.
    """
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "")
    state, base_dir = _new_state(tmp_path)
    _request(state, HOST)
    assert _answer(state, HOST, "允许") is True

    reopened = _reopen(state, base_dir)
    with pytest.raises(resource_fetch.NetworkAccessRequired) as first:
        _require(reopened, HOST)
    assert first.value.as_result(kind="git")["error_code"] == "network_access_required"
    # The model may retry fetch before re-requesting; that pops no card.
    with pytest.raises(resource_fetch.NetworkAccessRequired) as again:
        _require(reopened, HOST)
    assert again.value.as_result(kind="git")["error_code"] == "network_access_required"

    _request(reopened, HOST)
    # An approval whose write-back never reached this live State.
    reopened.append_transcript(
        "capability_grant_answered", host=HOST, granted=True, answer_unreadable=False,
    )
    with pytest.raises(resource_fetch.NetworkAccessRequired) as stuck:
        _require(reopened, HOST)
    refusal = stuck.value.as_result(kind="git")
    assert refusal["error_code"] == "network_access_request_unresolved", refusal
    assert "request_network_access" not in refusal
    assert refusal["next_action"]["tool"] == "report_blocker"


def test_a_pending_request_still_does_not_ask_for_a_second_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1069：only a lapsed approval reopens the request path; unanswered stays put."""
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "")
    state, base_dir = _new_state(tmp_path)
    _request(state, HOST)

    reopened = _reopen(state, base_dir)
    with pytest.raises(resource_fetch.NetworkAccessRequired) as caught:
        _require(reopened, HOST)
    refusal = caught.value.as_result(kind="git")
    assert refusal["error_code"] == "network_access_request_unresolved", refusal
    assert "request_network_access" not in refusal
