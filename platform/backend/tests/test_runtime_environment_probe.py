"""执行环境健康：探针在启动时抓住坏环境，归因层对漏网的说真话。

## 现场（2026-08-21）

`.venv/bin/python` 指着已卸载的 anaconda —— 唯一活着的 worker 被登出杀掉后，
用户每条消息都撞 "HARNESS_PYTHON is not an executable file"，且被兜底文案
翻译成「平台在记录这次运行时撞上了内部错误，再发一次即可」：不在记录阶段、
不是内部错误、重发一百次都没用。

设计公理：**用户永远不该是第一个发现执行环境坏了的人。** 环境的前置条件在
部署时建立，就必须在启动时验证（probe_runtime_environment）；运行期间坏掉
这种漏网情况，归因层必须报出真实身份与出路（runtime_environment_broken）。
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from app.config import settings


@pytest.mark.asyncio
async def test_sandbox_startup_probe_preserves_a_venv_interpreter_symlink(
    tmp_path, monkeypatch
):
    root = tmp_path / "harness"
    root.mkdir()
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    observed: dict[str, object] = {}

    class Probe:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def spawn(*argv, **kwargs):
        observed.update(argv=argv, kwargs=kwargs)
        return Probe()

    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_root", str(root))
    monkeypatch.setattr(settings, "harness_python", str(interpreter))
    from app import main as app_main

    monkeypatch.setattr(app_main.asyncio, "create_subprocess_exec", spawn)
    await app_main._record_what_this_machine_enforces()

    assert Path(observed["argv"][0]) == interpreter.absolute()


@pytest.mark.asyncio
async def test_probe_names_a_missing_harness_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "harness_root", str(tmp_path / "nowhere"))
    monkeypatch.setattr(settings, "harness_python", sys.executable)
    from app.services.harness_sessions import probe_runtime_environment

    problem = await probe_runtime_environment()
    assert problem is not None and "HARNESS_ROOT" in problem
    # 出路必须随问题一起给出，不让人去猜。
    assert "uv sync" in problem


@pytest.mark.asyncio
async def test_probe_names_a_dangling_interpreter(tmp_path, monkeypatch):
    """断链的 venv python（anaconda 卸载）必须被点名，而不是等用户撞上。"""
    root = tmp_path / "harness"
    (root / "core").mkdir(parents=True)
    (root / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "harness_root", str(root))
    monkeypatch.setattr(settings, "harness_python", str(tmp_path / "gone" / "python3"))
    from app.services.harness_sessions import probe_runtime_environment

    problem = await probe_runtime_environment()
    assert problem is not None and "HARNESS_PYTHON" in problem


@pytest.mark.asyncio
async def test_probe_passes_a_working_runtime(tmp_path, monkeypatch):
    """健康判据是「worker 解释器真的能 import platform_runtime」，不是路径长得像。"""
    root = tmp_path / "harness"
    (root / "core").mkdir(parents=True)
    (root / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    (root / "platform_runtime.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "harness_root", str(root))
    monkeypatch.setattr(settings, "harness_python", sys.executable)
    from app.services.harness_sessions import probe_runtime_environment

    assert await probe_runtime_environment() is None


@pytest.mark.asyncio
async def test_probe_reports_an_interpreter_that_cannot_import(tmp_path, monkeypatch):
    root = tmp_path / "harness"
    (root / "core").mkdir(parents=True)
    (root / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    (root / "platform_runtime.py").write_text(
        textwrap.dedent("""
        import a_module_that_does_not_exist_anywhere
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "harness_root", str(root))
    monkeypatch.setattr(settings, "harness_python", sys.executable)
    from app.services.harness_sessions import probe_runtime_environment

    problem = await probe_runtime_environment()
    assert problem is not None and "起不来" in problem


def test_broken_runtime_reaches_the_user_with_its_real_identity():
    """漏网到运行期的环境故障，用户读到的必须是真话，不是「再发一次即可」。"""
    from app.services.harness_sessions import HarnessSessionError
    from app.services.run_failures import describe

    described = describe(
        HarnessSessionError(
            "HARNESS_PYTHON is not an executable file",
            code="runtime_environment_broken",
        )
    )
    assert described.code == "runtime_environment_broken"
    assert described.title == "平台的执行环境坏了"
    # 环境修好之前重发一定还是这个结果 —— 三态里的 False，不给假希望。
    assert described.retryable is False


def test_changing_bindings_during_a_turn_is_not_an_error_at_all():
    """在飞的 turn 换绑定 —— **不再是一个错误**（RFC D10 删除清单，08-23）。

    这条测试原来叫 `..._speaks_as_session_busy`，验的是那句拒绝措辞对不对
    （8-21 之前它是一句无 code 的散文，掉进兜底变成「平台内部错误」）。

    可那次修的是"话说得对不对"，而这件事**根本不该是一句拒绝**：用户换了
    模型、又说了句话，两个动作都完全合法。现在的答案是 `defer` —— 这一轮用
    旧绑定跑完，下一轮自然换代。所以判据也换了：不是"错误措辞对不对"，
    是"它压根不产生错误"。
    """
    from app.services.harness_sessions import worker_reuse_decision

    assert worker_reuse_decision(
        same_bindings=False, conversation_in_flight=True
    ) == "defer"


def test_the_startup_gate_is_awaitable_and_lifespan_kept_its_decorator():
    """2026-08-21 部署现场：插入闸函数时把 @asynccontextmanager 顶到了它头上
    —— 装饰器套错对象，lifespan 变裸生成器、闸变 context manager，启动当场
    TypeError。测试没抓到是因为没有任何测试真正走过 lifespan 的这几行。
    这里把两边的类型钉死：谁再在装饰器与 lifespan 之间插东西都会红。"""
    import inspect

    from app import main as app_main

    assert inspect.iscoroutinefunction(app_main._refuse_to_serve_with_a_broken_runtime)
    manager = app_main.lifespan(app_main.app)
    assert hasattr(manager, "__aenter__") and hasattr(manager, "__aexit__")
