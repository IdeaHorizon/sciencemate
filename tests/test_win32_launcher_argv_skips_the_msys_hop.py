"""兼容入口交给调用方的 win32 argv 必须是启动器本身，不能先过一跳 MSYS bash（#943）。

现场（Windows 真机，0.4.3；2026-10-09 在 main 上原样复现）：``submit_job scheduler=local``
提交成功，作业退 64，stderr 52 字节＝``_win32_exec.py`` 的 usage 一行 + CRLF，载荷一行没跑。

病根：``core.sandbox._native_launch`` 给每个后端的 argv 套一层
``posix_shell() -c 'cd -- "$0" && exec "$@"' <cwd> …``，好让不传 cwd 的老调用方也落在 cwd。
Windows 上 ``posix_shell()`` 是 MSYS bash；原生 python 起它时，MSYS 运行时按 Cygwin 规则
重新解析命令行 —— 引号里的 ``\\\\`` 被折成 ``\\``（真机实测，``{a}`` 还会丢括号）。win32 启动器的
JSON spec 里全是 ``C:\\\\Users\\\\…``，过这一跳就不再是合法 JSON，启动器 ``json.loads`` 失败、
打 usage、退 64。detached 与前台兼容入口（safe_bash 的 git 审计、build_graph）同一条路。

修法：win32 启动器自己从 spec 里取 cwd、自己进（``_win32_exec.run``），``NativeLaunch``
标 ``cwd_in_argv``，``_native_launch`` 见到它就不再套 shell。

这里在任何宿主上都能跑：走真的 ``select_backend("win32")`` + 真 ``Win32Backend.prepare``
（prepare 不起进程）。「启动器真的进了 cwd、作业真的跑完」由 tests/test_native_jobs.py
在 Windows 真机上判（不加 skipif 之外的条件）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from core import isolation, sandbox
from shared.lib import shell

BASH = r"C:\Program Files\Git\usr\bin\bash.exe"


@pytest.fixture(autouse=True)
def _win32_backend(tmp_path, monkeypatch):
    monkeypatch.setenv(isolation.EXECUTOR_ENV, "win32")
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


def _assert_is_the_launcher_with_its_own_cwd(argv: list[str], work: Path, payload: list[str]):
    assert argv[0] == sys.executable, (
        f"a shell ({argv[0]}) sits in front of the win32 launcher; on Windows that is "
        "MSYS bash, which re-parses and corrupts the launcher's JSON spec")
    assert shell.posix_shell() not in argv[:argv.index("--")]
    assert Path(argv[3]).name == "_win32_exec.py" and argv[4] == "run"
    spec = json.loads(argv[5])
    assert spec["cwd"] == str(work.resolve())
    assert argv[6:] == ["--", *payload]


def test_foreground_compat_launch_is_the_launcher_carrying_its_cwd(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    payload = [BASH, "experiment_job.sh"]

    launch = sandbox.prepare_attempt_command(
        payload, state=None, cwd=work, writable_roots=[work])

    _assert_is_the_launcher_with_its_own_cwd(list(launch.argv), work, payload)


def test_detached_job_launch_json_is_the_launcher_carrying_its_cwd(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    payload = [BASH, str(work / "experiment_job.sh")]

    launch = sandbox.prepare_launch(
        payload, cwd=work, writable_roots=[work], detached=True,
        stdout_path=work / "job.out", stderr_path=work / "job.err")

    recorded = json.loads((launch.control_dir / "launch.json").read_text(encoding="utf-8"))
    _assert_is_the_launcher_with_its_own_cwd(recorded["argv"], work, payload)


def test_posix_backends_keep_the_cd_wrapper(tmp_path, monkeypatch):
    """不传 cwd 的老调用方在 POSIX 上照旧靠这层 cd —— 只有自己进 cwd 的启动器才免。"""
    native = isolation.native_backend_name()
    if native not in {"darwin", "linux"}:
        pytest.skip("needs a POSIX native backend to build the argv")
    monkeypatch.setenv(isolation.EXECUTOR_ENV, native)
    isolation._reset_for_tests()
    work = tmp_path / "work"
    work.mkdir()

    launch = sandbox.prepare_attempt_command(
        ["/bin/echo", "hi"], state=None, cwd=work, writable_roots=[work])

    assert launch.argv[:4] == [shell.posix_shell(), "-c", 'cd -- "$0" && exec "$@"',
                               str(work.resolve())]
