"""档位是显式声明：协作每张卡都停；自主与连续自动放行非高危决策卡；只有连续绕行高危。

wangd 2026-09-09 拍板「自主档也自动放行非高危的决策卡」。此前 worker 只认类别列表，
自主档不预授权任何类别时列表是空的，与协作在 worker 眼里没有区别（node20 上自主≈协作）。
现在档位随每条请求显式下发（`autonomy_mode`），类别列表只是它的投影。
"""
from __future__ import annotations

from types import SimpleNamespace

from core import pause_driver, session_driver
from shared.lib import dangerous_commands as dc


def _state(**hook):
    return SimpleNamespace(hook_state=dict(hook))


def _pause(kind: str):
    return SimpleNamespace(metadata={"type": kind})


def test_autonomous_with_no_preauthorised_classes_still_approves_decisions():
    assert session_driver.apply_autonomy(
        _state(autonomy_mode="autonomous", authorized_risk_classes=[])
    ) is False
    assert pause_driver.AUTO_APPROVE_ENABLED is True, "自主档的决策卡没有自动放行"
    assert dc.BYPASS_ENABLED is False, "自主档不许绕行高危确认"
    assert pause_driver._is_high_risk(_pause("highrisk_confirm")) is True
    assert pause_driver._is_high_risk(_pause("decision_package")) is False


def test_assisted_stops_at_every_card():
    session_driver.apply_autonomy(_state(autonomy_mode="assisted", authorized_risk_classes=[]))
    assert pause_driver.AUTO_APPROVE_ENABLED is False
    assert dc.BYPASS_ENABLED is False


def test_continuous_approves_everything():
    assert session_driver.apply_autonomy(
        _state(autonomy_mode="continuous", authorized_risk_classes=["*"])
    ) is True
    assert pause_driver.AUTO_APPROVE_ENABLED is True and dc.BYPASS_ENABLED is True


def test_without_an_explicit_declaration_the_class_list_still_tells_the_tier():
    """CLI 表的约定：空 = 协作，非空 = 自主，["*"] = 连续。"""
    assert session_driver.autonomy_mode(_state()) == "assisted"
    assert session_driver.autonomy_mode(_state(authorized_risk_classes=["shell_write"])) == "autonomous"
    assert session_driver.autonomy_mode(_state(authorized_risk_classes=["*"])) == "continuous"
    assert session_driver.autonomy_mode(_state(autonomy_mode="assisted", authorized_risk_classes=["x"])) == "assisted"


def test_the_worker_stores_the_declared_tier_and_applies_it():
    import platform_runtime

    session = platform_runtime.PlatformSession.__new__(platform_runtime.PlatformSession)
    session.state = SimpleNamespace(hook_state={}, append_transcript=lambda *a, **k: None)
    session._authorized_risk_classes = []
    session._autonomy_mode = None
    session.declare_authorization([], "autonomous")
    assert session.state.hook_state["autonomy_mode"] == "autonomous"
    assert session._autonomy_mode == "autonomous"
    assert pause_driver.AUTO_APPROVE_ENABLED is True
    session.declare_authorization([], "assisted")
    assert pause_driver.AUTO_APPROVE_ENABLED is False


def test_the_cli_declares_by_name_and_projects_the_classes():
    import chat

    state = SimpleNamespace(hook_state={})
    assert chat._set_autonomy_mode(state, "continuous") is True
    assert state.hook_state["autonomy_mode"] == "continuous"
    assert state.hook_state["authorized_risk_classes"] == ["*"]
    assert chat._set_autonomy_mode(state, "autonomous") is False
    assert pause_driver.AUTO_APPROVE_ENABLED is True
    assert "非高危决策自动放行" in chat._autonomy_label(state)
