"""档位随每条请求显式送到 worker；自主 / 连续切档都替人点掉屏幕上那张。"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from app.services import local_execution
from app.services.local_execution import AutonomyPolicy

_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_the_policy_names_the_tier() -> None:
    assert AutonomyPolicy().mode == "assisted"
    assert AutonomyPolicy(authorized_risk_classes=("*",), mode="continuous").mode == "continuous"


@pytest.mark.asyncio
async def test_autonomous_without_classes_is_still_autonomous(monkeypatch) -> None:
    """node20 缺资源上限只降"无人值守续轮"，不降档位。"""
    from app.models.project import OperationMode

    class _DB:
        async def scalar(self, _stmt):
            return SimpleNamespace(
                operation_mode=OperationMode.AUTONOMOUS, autonomous_authorized_risk_classes=[]
            )

    monkeypatch.setattr(local_execution, "_machine_missing_for_unattended", lambda: ["disk_cap"])
    monkeypatch.setattr("app.assembly.weak_resource_walls_are_acceptable", lambda: False)
    policy = await local_execution.project_autonomy_policy(_DB(), SimpleNamespace(id="p"))
    assert policy.unattended is False and policy.blocked_reason
    assert policy.mode == "autonomous"
    assert policy.authorized_risk_classes == ()


def test_every_worker_request_carries_the_tier() -> None:
    source = (_BACKEND / "app" / "services" / "harness_sessions.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    rpc = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_rpc_locked"
    )
    keys = {
        k.value for node in ast.walk(rpc) if isinstance(node, ast.Dict)
        for k in node.keys if isinstance(k, ast.Constant)
    }
    assert {"authorized_risk_classes", "autonomy_mode"} <= keys


def test_switching_to_autonomous_answers_the_pending_card_too() -> None:
    source = (_BACKEND / "app" / "api" / "v1" / "projects.py").read_text(encoding="utf-8")
    assert 'if policy.mode != "assisted":' in source
    assert '"*" in policy.authorized_risk_classes' not in source
