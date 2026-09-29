"""Run-local dependency installation is part of the container contract."""

from __future__ import annotations

import sys

from pathlib import Path

import pytest

from core.state import State
from shared.tools.library.python_exec import (
    _execute_python,
    _install_requirements,
)


def _state(tmp_path: Path) -> State:
    return State(run_id="r1", node_type="experiment", root=tmp_path / "runstate")


@pytest.mark.asyncio
async def test_requirements_are_resolved_inside_the_image_not_from_host_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host site-packages must never decide what exists in the sandbox image."""
    import shared.tools.library.python_exec as pe

    calls: list[list[str]] = []

    async def _record(*args, **kwargs):
        calls.append(list(args))
        return "done", 0, b"OK\n", b""

    monkeypatch.setattr(pe, "spawn_and_wait", _record)

    result = await _execute_python(
        state=_state(tmp_path), code="import sys; print('OK', sys.version_info[0])",
        requirements=["pytest"], timeout=60,
    )

    assert result["status"] == "success"
    installer_calls = [c for c in calls if "install" in c]
    assert len(installer_calls) == 1
    assert installer_calls[0][0:3] == [sys.executable, "-m", "pip"]  # harness venv 的解释器，不是 PATH 上碰巧的 python3
    assert "--target" in installer_calls[0]
    assert calls[-1][0:2] == [sys.executable, "-c"]


@pytest.mark.asyncio
async def test_networked_resolver_cannot_see_project_or_run_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[tuple, dict]] = []

    async def _fake(*args, **kwargs):
        seen.append((args, kwargs))
        return "done", 0, b"", b""

    monkeypatch.setattr("shared.tools.library.python_exec.spawn_and_wait", _fake)
    target = tmp_path / "runstate/.harness/python-packages"
    assert await _install_requirements(["some_pkg"], _state(tmp_path), target) is None
    assert len(seen) == 2
    download_args, download = seen[0]
    install_args, install = seen[1]
    assert download_args[:4] == (sys.executable, "-m", "pip", "download")
    assert "--only-binary=:all:" in download_args
    assert download["network_access"] is True
    assert len(download["writable_roots"]) == 1
    assert download["readonly_roots"] == []
    assert target not in download["writable_roots"]
    assert Path(download["writable_roots"][0]).parent == target.parent
    assert install_args[:4] == (sys.executable, "-m", "pip", "install")
    assert "--no-index" in install_args
    assert install["network_access"] is False
    assert install["writable_roots"] == [target]


@pytest.mark.asyncio
async def test_installer_failure_names_the_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v19 只给了一句"pip install 失败"，看不出缺什么、也看不出装包器坏了。"""
    async def _fake(*args, **kwargs):
        return "done", 1, b"", b"boom"

    monkeypatch.setattr("shared.tools.library.python_exec.spawn_and_wait", _fake)

    failure = await _install_requirements(
        ["pkg_a", "pkg_b"], _state(tmp_path), tmp_path / "runstate/deps")

    assert failure is not None
    assert failure["missing"] == ["pkg_a", "pkg_b"]
    assert "pkg_a" in failure["error"] and "pkg_b" in failure["error"]
    assert failure["installer_error"] == "boom"


@pytest.mark.asyncio
async def test_requirement_urls_and_pip_arguments_are_rejected_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _must_not_spawn(*args, **kwargs):
        raise AssertionError("invalid dependency reached pip")

    monkeypatch.setattr("shared.tools.library.python_exec.spawn_and_wait", _must_not_spawn)
    failure = await _install_requirements(
        ["-r /etc/passwd", "pkg @ https://example.com/pkg.whl"],
        _state(tmp_path),
        tmp_path / "runstate/deps",
    )
    assert failure and len(failure["invalid"]) == 2


@pytest.mark.asyncio
async def test_a_package_that_is_already_there_survives_a_broken_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装不上 ≠ 没有。

    2026-09-17 实测：postprocess 的 TikZ 后端声明 pypdfium2>=4.0，包在解释器里
    装着（5.13.0、能 import），但那个 venv 没有 pip —— 「装不上」被当成「没有」，
    铸 figure 记录被拒，agent 花掉一整轮查环境最后 report_blocker。

    判据要问**代码将来会看到的那份真相**，不是装包器跑不跑得起来。
    """

    import shared.tools.library.python_exec as pe

    async def _installer_always_fails(*_args, **_kwargs):
        return {"status": "error", "error": "No module named pip", "missing": ["x"]}

    monkeypatch.setattr(pe, "_install_requirements", _installer_always_fails)

    ran: list[str] = []

    async def _spawn(*args, **kwargs):
        code = args[2] if len(args) > 2 else ""
        if "importlib.metadata" in code and "canonicalize_name" in code:
            return "done", 0, b'[]\n', b""      # 沙箱说：都在
        ran.append(code)
        return "done", 0, b"OK\n", b""

    monkeypatch.setattr(pe, "spawn_and_wait", _spawn)

    result = await _execute_python(
        state=_state(tmp_path), code="print('OK')",
        requirements=["pypdfium2>=4.0"], timeout=60,
    )
    assert result["status"] == "success", result
    assert ran, "代码本身必须真的跑过"

    # 反过来：沙箱说确实缺，就得如实失败（否则这道闸等于没有）
    async def _spawn_missing(*args, **kwargs):
        code = args[2] if len(args) > 2 else ""
        if "importlib.metadata" in code and "canonicalize_name" in code:
            return "done", 0, b'["nope>=1"]\n', b""
        return "done", 0, b"OK\n", b""

    monkeypatch.setattr(pe, "spawn_and_wait", _spawn_missing)
    failed = await _execute_python(
        state=_state(tmp_path), code="print('OK')",
        requirements=["nope>=1"], timeout=60,
    )
    assert failed["status"] == "error", failed
    assert failed["missing"] == ["nope>=1"], failed
