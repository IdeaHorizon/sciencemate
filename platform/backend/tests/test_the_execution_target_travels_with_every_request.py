"""执行目标（本机 / ``wsl:<发行版>``）：项目设置 → 派发投影 → 每条请求 → worker → 冻进 attempt。

RFC_WINDOWS_EXECUTION_TARGETS D1/D2。与档位同一条通道：每次派发读一次配置，``_authorize``
记到会话上，``_rpc_locked`` 挂在每一条请求上；改设置后的广播把它推给正在跑的会话。
存进去之前在接口里核：写法对不对、这台机器上有没有这个发行版、ssh 还没做。
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from app.models.project import OperationMode
from app.services import execution_targets, local_execution
from app.services.local_execution import AutonomyPolicy

_BACKEND = pathlib.Path(__file__).resolve().parents[1]


class _Db:
    def __init__(self, config):
        self._config = config

    async def scalar(self, _query):
        return self._config


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [OperationMode.ASSISTED, OperationMode.AUTONOMOUS])
async def test_the_policy_carries_the_target_in_every_tier(monkeypatch, mode) -> None:
    monkeypatch.setattr(local_execution, "_machine_missing_for_unattended", lambda: [])
    config = SimpleNamespace(operation_mode=mode, autonomous_authorized_risk_classes=["compute"],
                             execution_target="wsl:Ubuntu-24.04")
    policy = await local_execution.project_autonomy_policy(_Db(config), SimpleNamespace(id="p"))
    assert policy.execution_target == "wsl:Ubuntu-24.04"


@pytest.mark.asyncio
async def test_no_setting_means_this_machine(monkeypatch) -> None:
    config = SimpleNamespace(operation_mode=OperationMode.ASSISTED,
                             autonomous_authorized_risk_classes=None, execution_target=None)
    policy = await local_execution.project_autonomy_policy(_Db(config), SimpleNamespace(id="p"))
    assert policy.execution_target == "local"
    assert AutonomyPolicy().execution_target == "local"


def test_every_worker_request_carries_the_target() -> None:
    source = (_BACKEND / "app" / "services" / "harness_sessions.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    rpc = next(n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_rpc_locked")
    keys = {k.value for node in ast.walk(rpc) if isinstance(node, ast.Dict)
            for k in node.keys if isinstance(k, ast.Constant)}
    assert "execution_target" in keys


def test_the_session_takes_the_target_from_the_dispatch_policy() -> None:
    from app.services.harness_sessions import _ProjectHarnessSession

    session = object.__new__(_ProjectHarnessSession)
    session.execution_target = "local"
    session._authorize(AutonomyPolicy(execution_target="wsl:Ubuntu-24.04"))
    assert session.execution_target == "wsl:Ubuntu-24.04"
    session._authorize(None)                       # 生命周期类操作不改它
    assert session.execution_target == "wsl:Ubuntu-24.04"


def test_the_frozen_attempt_names_the_target() -> None:
    source = (_BACKEND / "app" / "services" / "local_execution.py").read_text(encoding="utf-8")
    assert "attempt_capability(target=execution_target)" in source
    assert "execution_target=autonomy.execution_target" in source


# ── 存进去之前核一遍 ───────────────────────────────────────────────────────────


def test_local_is_stored_as_null() -> None:
    assert execution_targets.normalise("local") is None
    assert execution_targets.normalise(None) is None


@pytest.mark.parametrize("raw", ["wsl", "wsl:a b", "docker:x"])
def test_bad_spellings_are_refused(raw) -> None:
    with pytest.raises(execution_targets.TargetRejectedError) as caught:
        execution_targets.normalise(raw)
    assert caught.value.message["zh"] and caught.value.message["en"]


def test_ssh_is_refused_until_it_exists() -> None:
    with pytest.raises(execution_targets.TargetRejectedError, match="ssh"):
        execution_targets.normalise("ssh:hpc")


def test_wsl_needs_windows_and_a_registered_distro(monkeypatch) -> None:
    monkeypatch.setattr(execution_targets.sys, "platform", "darwin")
    with pytest.raises(execution_targets.TargetRejectedError, match="Windows"):
        execution_targets.normalise("wsl:Ubuntu-24.04")
    monkeypatch.setattr(execution_targets.sys, "platform", "win32")
    fake = SimpleNamespace(list_distros=lambda: ["Ubuntu-24.04", "docker-desktop"])
    monkeypatch.setattr(execution_targets, "wsl_module", lambda: fake)
    assert execution_targets.normalise("wsl:Ubuntu-24.04") == "wsl:Ubuntu-24.04"
    with pytest.raises(execution_targets.TargetRejectedError, match="Debian"):
        execution_targets.normalise("wsl:Debian")


def test_the_choices_list_this_machine_and_the_distros(monkeypatch) -> None:
    assert execution_targets.available()["targets"][0]["value"] == "local"
    monkeypatch.setattr(execution_targets.sys, "platform", "win32")
    monkeypatch.setattr(execution_targets, "wsl_module",
                        lambda: SimpleNamespace(list_distros=lambda: ["Ubuntu-24.04"]))
    values = [t["value"] for t in execution_targets.available()["targets"]]
    assert values == ["local", "wsl:Ubuntu-24.04"]


def test_the_settings_endpoint_checks_before_storing() -> None:
    import inspect

    from app.api.v1 import projects as projects_api

    source = inspect.getsource(projects_api.update_project_config)
    check = source.index("normalise")
    store = source.index("setattr(config, field, value)")
    assert check < store, "先核后存：存进去一个起不来的目标，症状要到下一条命令才在 worker 里出现"
