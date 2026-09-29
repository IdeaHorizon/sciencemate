"""沙箱能力是记账与显示，不是启动条件（RFC_EXECUTOR_TIERS §3.4 / 个人版 X6）。

## 现场

2026-09-04 与 09-05 两次部署被同一道门挡住：node20 的内核参数
（`apparmor_restrict_unprivileged_userns=1`）让 bwrap 起不来，于是部署脚本判
「守不住 net_deny」→ 中止。main 上四个已合的修复因此到不了同事手里，而挡住它们
的既不是代码问题也不是数据问题。

个人档把这条推到极限：软件下载下来就得能打开。一台守不住写边界的机器上，
「打开界面看以前的项目」和「跑一个作业」是两件事，不该被同一个判决绑在一起。

## 判决该在哪一层

在**派发那一刻**：`core.isolation._resolve_auto` 仍然守着写边界（I1），守不住就
不把作业跑起来。那一层够得着现场，能说清楚是哪条不变量、这次要干什么。
启动这一层够不着，它只能一刀切。

这条测试钉住三件事：启动路径上没有沙箱造成的拒绝、部署脚本没有硬闸、
守不住的机器仍然 ready 并如实说自己守不住什么。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app import main as app_main
from app.config import settings
from tests.test_local_runtime_api import runtime_client  # noqa: F401 - fixture

DEPLOY = Path(app_main.__file__).resolve().parents[3] / "deploy" / "platform" / "deploy-node20.sh"


def test_the_boundary_recorder_raises_nothing() -> None:
    """默认拒绝：这个函数体里不许有任何 raise。

    扫的是「有没有 raise」而不是「有没有那句文案」—— 换一句话术就绕过的判据
    等于没有判据。
    """
    tree = ast.parse(inspect.getsource(app_main._record_what_this_machine_enforces))
    raises = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert not raises, (
        f"_record_what_this_machine_enforces 里还有 raise（相对行 {raises}）。"
        "记账函数不许决定服务起不起得来 —— 判决在派发那一刻。"
    )


def test_the_deploy_script_prints_the_boundary_and_does_not_gate_on_it() -> None:
    if not DEPLOY.exists():
        pytest.skip("内网部署脚本不在这棵树里（公开树）")
    script = DEPLOY.read_text(encoding="utf-8")
    assert "execution boundary" in script, "部署仍然要把这台机器守住了什么打出来"
    block_start = script.index("from core import isolation")
    block = script[block_start : script.index("\nPY", block_start)]
    assert "SystemExit" not in block, (
        "部署脚本又在沙箱能力上设硬闸了。守不住要打印警告，不是中止部署 —— "
        "09-04/09-05 两次就是被这句挡住的。"
    )


def test_readiness_does_not_consult_the_sandbox() -> None:
    """就绪判断里没有沙箱这一项 —— 一条都不许有。

    判据落在纯函数 `is_ready` 上，而不是 `/health/ready` 的 status：后者要先
    满足一堆与沙箱无关的前提（每张表都在、harness_root 指对），其中任何一个
    不满足，「把沙箱加回就绪条件」这个变异都照样绿。2026-09-05 实测踩到。
    """
    serving = {
        "database": "ok",
        "schema_tables": "ok",
        "alembic_version": "unmanaged",
        "startup_reconciliation": "ok",
        "harness_root": "ok",
    }
    assert app_main.is_ready(serving | {"sandbox": "ok"}, bridge_enabled=True) is True
    assert app_main.is_ready(
        serving | {"sandbox": "none: no native isolation backend for win32"},
        bridge_enabled=True,
    ) is True, "沙箱守不住让整台机器变成 degraded —— 它又是启动条件了"
    assert app_main.is_ready(serving | {"sandbox": "not_run"}, bridge_enabled=True) is True
    # 反过来：真正的就绪条件仍然管用，别把这条判断整个掏空。
    assert app_main.is_ready(serving | {"database": "error: refused"}, bridge_enabled=True) is False
    assert app_main.is_ready(serving | {"harness_root": "invalid"}, bridge_enabled=True) is False


@pytest.mark.asyncio
async def test_a_host_without_a_boundary_still_reports_what_it_cannot_enforce(
    runtime_client, monkeypatch,
) -> None:
    """ready 不等于假装守得住：检查项里照实说，界面据此显示。"""
    client, _factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", False)
    app_main.app.state.execution_boundary = {
        "backend": None, "enforced": [],
        "unavailable_reason": "no native isolation backend for win32",
    }
    app_main.app.state.sandbox_readiness = "none: no native isolation backend for win32"
    try:
        checks = (await client.get("/health/ready")).json()["checks"]
    finally:
        app_main.app.state.execution_boundary = None
        app_main.app.state.sandbox_readiness = "not_run"
    assert checks["sandbox"].startswith("none: ")
    assert checks["execution_boundary"]["unavailable_reason"]
