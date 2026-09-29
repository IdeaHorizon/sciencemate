"""Phase-1 compatibility matrix for Experiment's advisory stuck signals."""
from __future__ import annotations

from nodes.experiment import stuck_signals as signals
from core.bootstrap import bootstrap
from core.loop_hooks import get_loop_hook


def test_six_detector_sources_share_one_candidate_cycle_and_priority_order():
    state: dict = {}
    for source, priority in (
        ("generic_failure", 21),
        ("repeated_error", 30),
        ("strategic_review", 40),
        ("execution_control", 20),
        ("health_tick", 50),
        ("plan_before_compile", 10),
    ):
        signals.offer(
            state, source=source, priority=priority,
            content=f"{source} advice", dedupe_key=source,
        )

    selected, suppressed, count = signals.choose(
        state, turn=8, operational=False, cooldown_turns=5,
    )

    assert count == 6
    assert suppressed == []
    assert selected is not None
    assert selected.source == "plan_before_compile"
    assert selected.priority == 10
    assert selected.content == "plan_before_compile advice"


def test_same_adapter_refines_its_own_candidate_without_erasing_another_source():
    state: dict = {}
    signals.offer(state, source="repeated_error", priority=30,
                  content="old evidence", dedupe_key="old")
    signals.offer(state, source="strategic_review", priority=40,
                  content="route review", dedupe_key="route")
    signals.offer(state, source="repeated_error", priority=30,
                  content="new evidence", dedupe_key="new")

    selected, _, count = signals.choose(
        state, turn=8, operational=False, cooldown_turns=5,
    )

    assert count == 2
    assert selected is not None
    assert selected.source == "repeated_error"
    assert selected.content == "new evidence"


def test_cooldown_and_operation_scope_are_applied_by_one_shared_store():
    state: dict = {}
    signals.offer(state, source="repeat", priority=30,
                  content="change direction", dedupe_key="same")
    first, _, _ = signals.choose(state, turn=1, operational=False, cooldown_turns=5)
    assert first is not None
    signals.mark_injected(state, first, turn=1)

    signals.offer(state, source="repeat", priority=30,
                  content="change direction", dedupe_key="same")
    selected, suppressed, _ = signals.choose(
        state, turn=2, operational=False, cooldown_turns=5,
    )
    assert selected is None
    assert [(item.source, item.reason) for item in suppressed] == [("repeat", "cooldown")]

    signals.offer(state, source="scientific", priority=1,
                  content="scientific closure", dedupe_key="closure", scope="scientific")
    signals.offer(state, source="safety", priority=20,
                  content="inspect log", dedupe_key="log")
    selected, suppressed, _ = signals.choose(
        state, turn=6, operational=True, cooldown_turns=5,
    )
    assert selected is not None and selected.source == "safety"
    assert [(item.source, item.reason) for item in suppressed] == [
        ("scientific", "operation_scope")]


def test_resumed_legacy_state_migrates_without_losing_a_pending_candidate():
    state = {
        signals.LEGACY_PENDING_KEY: {
            "legacy": {
                "source": "legacy", "priority": "not-a-number",
                "content": "fallback advice", "dedupe_key": "legacy:key",
            },
        },
        signals.LEGACY_LAST_INJECTED_KEY: "corrupt",
    }

    selected, suppressed, count = signals.choose(
        state, turn=4, operational=False, cooldown_turns=5,
    )

    assert count == 1
    assert suppressed == []
    assert selected is not None
    assert selected.priority == 99
    signals.mark_injected(state, selected, turn=4)
    assert state[signals.LAST_INJECTED_KEY]["legacy:key"] == 4
    assert state[signals.LEGACY_LAST_INJECTED_KEY]["legacy:key"] == 4


def test_phase_one_keeps_detector_adapter_lifecycles_registered():
    """Consolidation changes shared state only, not where each detector runs."""
    bootstrap(force=True)
    expected = {
        "generic_failure_detector": (True, False, True),
        "repeated_error_detector": (True, True, False),
        "strategic_review_injector": (True, True, False),
        "execution_control": (True, True, True),
        "critical_rules_reminder": (True, False, False),
        "plan_before_compile": (False, True, False),
    }
    for name, lifecycle in expected.items():
        hook = get_loop_hook(name)
        assert hook is not None, name
        assert (hook.on_turn_start is not None,
                hook.on_turn_end is not None,
                hook.on_end is not None) == lifecycle


def test_signal_module_has_no_terminal_artifact_or_detector_imports():
    source = __import__("inspect").getsource(signals)
    assert "repair_ledger" not in source
    assert "save_artifact" not in source
    assert "from . import hooks" not in source
