"""Repository-wide pytest boundaries.

PYTEST_DONT_REWRITE — fixtures are also loaded by the isolated backend pytest root.

模型命令在测试里也走真墙：本机 darwin 用 seatbelt，Linux runner 用 Landlock（CI
容器里可用，bwrap 通常不可用），后端由 ``core.isolation`` 按 ``HARNESS_EXECUTOR=auto``
选。Docker 年代这里有一个"CI 进程替身"把 Docker 换成本机进程，PR C 随 Docker 一起删了：
没有替身，测试跑的就是生产跑的那道墙。

``production_sandbox`` 这个 marker 的原意是「exercise the unmodified fail-closed Docker
sandbox path」—— 那条路没有了。还带着它的测试都在 experiment 节点（owner lujy）：它们
断言的是容器的长相（control socket 藏没藏、hardened 探针、docker pids 事件）。按节点
owner 红线不代改，这里按 marker 的本意机械跳过，并把去处写在 reason 里（issue #793）。
"""
from __future__ import annotations

import importlib.util
import sys

import pytest

#: 这些是 pyproject.toml 里的 **base dependencies** —— 缺任何一个都说明跑测试的
#: 不是项目环境，而不是"少一个可选能力"。
_BASE_DEPS = ("sympy", "mpmath", "tiktoken", "tree_sitter", "numpy", "yaml")


def pytest_configure(config) -> None:
    """跑错解释器要一次说清楚，不要变成一墙无关的红（2026-09-10）。

    ``pytest`` 曾经只在 ``[project.optional-dependencies] test`` 里，于是 ``.venv``
    里没有它，而 ``uv run pytest`` 在项目环境里找不到时会**退到 PATH 上的那一个**：

        uv run python  → .venv/bin/python3             (依赖齐)
        uv run pytest  → /Library/Frameworks/.../3.12  (系统 python，零项目依赖)

    实测后果是 ``uv run pytest tests/`` **48 条红**，而其中一条真缺陷都没有 ——
    27 条报「推导验证需要 sympy」（sympy 是 base dep，`.venv` 里装着）、12 条
    ModuleNotFound、3 条缺 dotenv。一堵长期红的墙会让"红"这件事不再指向任何东西，
    而且它**看起来完全像是产品坏了**。

    真修法是把 pytest 放进 uv 默认安装的 ``[dependency-groups] dev``，让"退到别处"
    这件事不成立。这道检查是给**绕开 uv 直接敲 pytest** 的情形留的：它不兜任何底，
    只是把 N 条误导性的失败换成一句准确的话。
    """
    del config
    missing = [name for name in _BASE_DEPS if importlib.util.find_spec(name) is None]
    if not missing:
        return
    raise pytest.UsageError(
        f"这个解释器（{sys.executable}）没有本项目的 base dependencies："
        f"{', '.join(missing)}。\n"
        "它们在 pyproject.toml 的 [project.dependencies] 里，也就是说**跑测试的不是"
        "项目环境** —— 接着跑会得到一墙与真实缺陷无关的红。\n"
        "用 `uv run pytest ...`（pytest 在默认的 dev 组里，会用 .venv 里的那个）。"
    )

_SKIP = pytest.mark.skip(
    reason="production_sandbox = the Docker sandbox path, removed in PR C; "
           "rewrite against core.isolation native backends (issue #793)"
)


def pytest_collection_modifyitems(items):
    for item in items:
        if item.get_closest_marker("production_sandbox") is not None:
            item.add_marker(_SKIP)


def pytest_configure(config):
    config.addinivalue_line("markers", "requires_posix_mode: needs POSIX executable or directory permission bits")
    config.addinivalue_line("markers", "requires_symlink: needs permission to create a real symbolic link")


@pytest.fixture(scope="session")
def _symlink_capability(tmp_path_factory):
    root = tmp_path_factory.mktemp("symlink-capability")
    target = root / "target"
    target.write_text("probe")
    try:
        (root / "link").symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            return False
        raise
    return (root / "link").read_text() == "probe"


@pytest.fixture(autouse=True)
def _require_declared_capabilities(request):
    if request.node.get_closest_marker("requires_symlink") is not None:
        if not request.getfixturevalue("_symlink_capability"):
            pytest.skip("This account cannot create symbolic links (WinError 1314)")


@pytest.fixture(autouse=True)
def _require_posix_mode(request):
    if request.node.get_closest_marker("requires_posix_mode") is None:
        return
    import os
    tmp_path = request.getfixturevalue("tmp_path")
    probe = tmp_path / 'permission-probe'
    probe.write_text('probe')
    probe.chmod(0o644)
    executable = os.access(probe, os.X_OK)
    probe.unlink()
    if executable:
        pytest.skip('Filesystem does not enforce POSIX execute bits; native process tests cover executable launch')


@pytest.fixture
def assert_private():
    """Check OS access controls, not POSIX mode bits emulated by Windows stat."""
    def check(path, *, protected=True):
        import json
        import os
        import stat
        import subprocess
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
            return
        script = """
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$acl = Get-Acl -LiteralPath $env:RELEASE_TEST_ACL_PATH
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object {
    @{sid=$_.IdentityReference.Value;type=$_.AccessControlType.ToString();rights=[int]$_.FileSystemRights}
})
@{protected=$acl.AreAccessRulesProtected;me=$me;rules=$rules} | ConvertTo-Json -Depth 4 -Compress
"""
        executable = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        proc = subprocess.Popen([executable, "-NoProfile", "-NonInteractive", "-Command", script],
                                env={**os.environ, "RELEASE_TEST_ACL_PATH": str(path)},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate(timeout=20)
        assert proc.returncode == 0, stderr.decode("utf-8", "replace")
        acl = json.loads(stdout)
        assert acl["protected"] == protected, acl
        assert {item["sid"] for item in acl["rules"]} == {acl["me"], "S-1-5-18"}, acl
        assert all(item["type"] == "Allow" and item["rights"] & 0x1F01FF == 0x1F01FF for item in acl["rules"]), acl
    from pathlib import Path
    return check
