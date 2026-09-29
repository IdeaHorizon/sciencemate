"""028 v2 审查：离线安装指引要走得通；护栏要抓住原 bug 句子「只改一处」的变体。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from nodes.experiment.tests.test_tool_description_schema_consistency import (
    _description_schema_errors,
    _runtime_tool_sets,
)
from nodes.experiment.tests.test_timeout_escalation import (  # noqa: F401
    requires_sandbox,
    runattempt_state,
)


# ── B-1：照文案离线安装要走得通 ─────────────────────────────────────────────

def test_offline_install_guidance_names_find_links() -> None:
    """--no-index 下 pip 只从 --find-links 解析依赖与构建后端；只把本地文件列成
    位置参数，带依赖的 wheel 与 sdist 都装不上（下面的真实沙箱用例）。"""
    visible, _ = _runtime_tool_sets()
    description = visible["fetch_resource"].description
    if "--no-index" in description:
        assert "--find-links" in description


def _record(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={digest},{len(data)}"


def _wheel(directory: Path, name: str, files: dict[str, bytes], requires: tuple[str, ...] = ()) -> Path:
    info = f"{name}-0.1.dist-info"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1\n" + "".join(
        f"Requires-Dist: {item}\n" for item in requires)
    files = {
        **files,
        f"{info}/METADATA": meta.encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: review\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    lines = [f"{item},{_record(data)}" for item, data in files.items()] + [f"{info}/RECORD,,"]
    files[f"{info}/RECORD"] = ("\n".join(lines) + "\n").encode()
    path = directory / f"{name}-0.1-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for item, data in files.items():
            archive.writestr(item, data)
    return path


@requires_sandbox
def test_offline_wheel_with_dependency_needs_find_links_in_the_real_sandbox(runattempt_state):  # noqa: F811
    from nodes.experiment.tools.safe_bash import _exec_and_log

    root = runattempt_state.execution_root
    acquired = root / "acquired"
    acquired.mkdir()
    _wheel(acquired, "rvdep028", {"rvdep028/__init__.py": b"X = 1\n"})
    main = _wheel(acquired, "rvmain028", {"rvmain028/__init__.py": b"from rvdep028 import X\n"},
                  requires=("rvdep028",))

    def run(command: str) -> dict:
        return asyncio.run(_exec_and_log(
            runattempt_state, command, cwd=str(root), timeout=120, sandbox_profile="bash"))

    if run("pip --version").get("returncode") != 0:
        pytest.skip("sandbox has no pip")
    # 依赖已经取到本地，只把主包列成位置参数（最自然的读法）→ 失败
    literal = run(f"pip install --no-index --target deps1 acquired/{main.name}")
    find_links = run("pip install --no-index --find-links acquired --target deps2 rvmain028")

    assert literal["returncode"] != 0 and "rvdep028" in literal["stderr_tail"], literal
    assert find_links["returncode"] == 0, find_links
    assert (root / "deps2" / "rvdep028" / "__init__.py").is_file()


# ── B-2：护栏要抓住原 bug 句子「只改一处」的变体 ────────────────────────────

def _errors_after_appending(guidance: str):
    visible, registered = _runtime_tool_sets()
    mutated = dict(visible)
    mutated["fetch_resource"] = replace(
        visible["fetch_resource"],
        description=visible["fetch_resource"].description + "\n" + guidance,
    )
    return _description_schema_errors(mutated, {**registered, **mutated})


@pytest.mark.parametrize(
    "guidance",
    [
        "安装普通 Python wheel 用 safe_execute_python requirements;",      # 半角分号
        "安装普通 Python wheel 用 safe_execute_python requirements.",      # 半角句号
        "安装普通 Python wheel 用 safe_execute_python requirements, 然后重试。",
        "安装普通 Python wheel 用 `safe_execute_python requirements`。",   # 整句一个代码段
        "安装普通 Python wheel 用 safe_execute_python requirements 参数。",  # 有「参数」没有「的」
        "安装普通 Python wheel 用 safe_execute_python 的 requirements。",   # 有「的」没有「参数」
        "安装普通 Python wheel 用 safe_execute_python（requirements=[...]）。",  # 全角括号
        "Install plain wheels with safe_execute_python requirements;",     # 英文
    ],
)
def test_guard_rejects_one_edit_variants_of_the_original_bug(guidance: str) -> None:
    errors = _errors_after_appending(guidance)
    assert any(
        error.owner == "fetch_resource"
        and error.target == "safe_execute_python"
        and error.parameter == "requirements"
        for error in errors
    ), [error.render() for error in errors]


def test_guard_rejects_parameterless_guidance_to_a_hidden_tool() -> None:
    errors = _errors_after_appending("超长输出改用 execute_python 处理。")
    assert any(
        error.owner == "fetch_resource" and error.target == "execute_python"
        for error in errors
    ), [error.render() for error in errors]


def test_node_owned_descriptions_do_not_point_to_hidden_edit_tools() -> None:
    """safe_write_file 由 safe_bash.py 的 replace(write_file, ...) 注册，描述仍写着
    「用 edit_file 更高效」，而 Experiment 看不到任何 edit 工具。"""
    visible, _ = _runtime_tool_sets()
    assert "edit_file" not in visible
    assert "edit_file" not in visible["safe_write_file"].description
